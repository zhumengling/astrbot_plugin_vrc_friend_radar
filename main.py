import math
import tempfile
import uuid
import asyncio
import json
import html
from pathlib import Path
import time
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.util import SessionController, session_waiter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.io import save_temp_img
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools
from astrbot.core.agent.message import UserMessageSegment, TextPart
from PIL import Image as PILImage, ImageDraw, ImageFilter, ImageFont

from .core.config import PluginConfig
from .core.db import RadarDB
from .core.monitor import MonitorService
from .core.repository import SearchRepository, SettingsRepository
from .core.search_state import SearchSession
from .core.utils import extract_world_id, format_location, infer_joinability
from .core.vrchat_client import VRChatClientError, VRChatNetworkError, VRChatTwoFactorRequiredError
from .core.world_cache import WorldCache


@dataclass(slots=True)
class SoulProfileSummary:
    friend_id: str
    display_name: str
    report_days: int
    effective_days: int
    generated_at: str
    sample_event_count: int
    top_worlds: list[dict]
    timeline_worlds: list[dict]
    active_periods: list[str]
    style_tags: list[str]
    resident_label: str
    ai_persona_text: str
    ai_fortune_text: str
    kindred_name: str
    kindred_id: str
    kindred_score: int
    overlap_world_count: int
    overview_text: str
    card_cover_url: str
    card_cover_local_path: str
    quick_commands: list[str]


@dataclass(slots=True)
class ProfileTargetOption:
    friend_id: str
    display_name: str
    status: str
    location: str


@dataclass(slots=True)
class ProfileTargetResolveResult:
    friend_id: str | None
    display_name: str
    options: list[ProfileTargetOption]


class VRCFriendRadarPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = RadarDB(self.cfg)
        self.settings_repo = SettingsRepository(self.cfg)
        self.search_repo = SearchRepository(self.cfg)
        self.world_cache = WorldCache(self.cfg.data_dir)
        self.monitor = MonitorService(self.cfg, self.db, self.settings_repo)
        self.monitor.set_event_callback(self._handle_monitor_events)
        self.monitor.set_loop_tick_callback(self._handle_loop_tick)
        self.monitor.set_notice_callback(self._handle_monitor_notice)
        self._search_sessions: dict[str, SearchSession] = {}
        self._daily_task_last_sent_date: dict[str, str] = {"daily_report": ""}
        self._translation_lock_map: dict[str, asyncio.Lock] = {}
        self._last_private_admin_sender_id: str = ""



    def _is_public_friend_request_allowed(self) -> bool:
        return self.settings_repo.get_allow_public_friend_request() if self.settings_repo else self.cfg.allow_public_friend_request

    def _set_public_friend_request_allowed(self, enabled: bool) -> None:
        self.settings_repo.set_allow_public_friend_request(enabled)

    def _escape_html(self, value: str | None) -> str:
        return html.escape(str(value or '').strip())

    def _get_card_font(self, size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        font_candidates = []
        if bold:
            font_candidates.extend([
                "msyhbd.ttc",
                "simhei.ttf",
                "Arial-Bold.ttf",
                "DejaVuSans-Bold.ttf",
            ])
        font_candidates.extend([
            "msyh.ttc",
            "simsun.ttc",
            "Arial.ttf",
            "DejaVuSans.ttf",
        ])
        for font_name in font_candidates:
            try:
                return ImageFont.truetype(font_name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _measure_text(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[int, int]:
        bbox = draw.textbbox((0, 0), text or " ", font=font)
        return int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])

    def _wrap_text(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
        raw_lines = str(text or '').splitlines() or ['']
        wrapped: list[str] = []
        for raw in raw_lines:
            line = raw.strip()
            if not line:
                wrapped.append('')
                continue
            current = ''
            for ch in line:
                candidate = f"{current}{ch}"
                width, _ = self._measure_text(draw, candidate, font)
                if current and width > max_width:
                    wrapped.append(current)
                    current = ch
                else:
                    current = candidate
            if current:
                wrapped.append(current)
        return wrapped or ['']

    def _draw_wrapped_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        fill: tuple[int, int, int],
        x: int,
        y: int,
        max_width: int,
        line_spacing: int,
    ) -> int:
        lines = self._wrap_text(draw, text, font, max_width)
        _, line_height = self._measure_text(draw, '????', font)
        for line in lines:
            draw.text((x, y), line, font=font, fill=fill)
            y += line_height + line_spacing
        return y

    def _draw_round_rect(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        radius: int,
        fill: tuple[int, int, int, int],
        outline: tuple[int, int, int, int] | None = None,
        width: int = 1,
    ) -> None:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    def _load_cover_image(self, path: str, size: tuple[int, int]) -> PILImage.Image | None:
        local_path = str(path or '').strip()
        if not local_path or not Path(local_path).exists():
            return None
        try:
            img = PILImage.open(local_path).convert('RGB')
            return img.resize(size, PILImage.Resampling.LANCZOS)
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] ??????????: {exc}")
            return None

    def _paste_card_cover(
        self,
        target: PILImage.Image,
        cover: PILImage.Image | None,
        box: tuple[int, int, int, int],
        *,
        radius: int = 28,
        opacity: int = 56,
        blur_radius: int = 6,
    ) -> None:
        if cover is None:
            return
        width = max(1, box[2] - box[0])
        height = max(1, box[3] - box[1])
        try:
            local = cover.resize((width, height), PILImage.Resampling.LANCZOS).convert('RGBA')
            if blur_radius > 0:
                local = local.filter(ImageFilter.GaussianBlur(blur_radius))
            alpha = PILImage.new('L', (width, height), color=max(0, min(255, opacity)))
            rounded_mask = PILImage.new('L', (width, height), 0)
            ImageDraw.Draw(rounded_mask).rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
            local.putalpha(PILImage.composite(alpha, PILImage.new('L', (width, height), 0), rounded_mask))
            target.alpha_composite(local, dest=(box[0], box[1]))
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] ??????????: {exc}")

    def _short_world_label(self, world_name: str) -> str:
        text = str(world_name or '').strip()
        if not text:
            return '??'
        simplified = re.sub(r'[\s\-_/|]+', '', text)
        simplified = re.sub(r'[()\[\]{}\u3010\u3011\u300c\u300d]', '', simplified)
        if re.search(r'[\u4e00-\u9fff]', simplified):
            chars = [ch for ch in simplified if re.search(r'[\u4e00-\u9fffA-Za-z0-9]', ch)]
            return ''.join(chars[:2]) or simplified[:2]
        compact = re.sub(r'[^A-Za-z0-9]', '', simplified)
        return (compact[:3] or simplified[:3]).upper()

    async def _build_timeline_worlds(self, event_rows: list, snapshot) -> list[dict]:
        now = datetime.now()
        max_nodes = 8
        visits: list[tuple[datetime, str]] = []
        last_world = ''
        had_break = True

        def push_visit(ts: datetime, world_id: str) -> None:
            nonlocal last_world, had_break
            if not world_id:
                last_world = ''
                had_break = True
                return
            if had_break or world_id != last_world:
                visits.append((ts, world_id))
            last_world = world_id
            had_break = False
        for item in reversed(event_rows):
            ts_text = str(item.created_at or '').strip()
            try:
                ts = datetime.fromisoformat(ts_text)
            except Exception:
                ts = now

            if item.event_type == 'location_changed':
                new_world = extract_world_id(item.new_value)
                if new_world:
                    push_visit(ts, new_world)
                elif extract_world_id(item.old_value):
                    push_visit(ts, '')
                continue

            value = item.new_value if item.event_type == 'friend_online' else ''
            world_id = extract_world_id(value)
            if world_id:
                push_visit(ts, world_id)

        if snapshot and snapshot.location:
            current_world = extract_world_id(snapshot.location)
            if current_world:
                try:
                    ts = datetime.fromisoformat(snapshot.updated_at) if snapshot.updated_at else now
                except Exception:
                    ts = now
                push_visit(ts, current_world)

        if len(visits) <= max_nodes:
            picked = visits
        else:
            picked: list[tuple[datetime, str]] = []
            last_index = len(visits) - 1
            for slot in range(max_nodes):
                idx = round(slot * last_index / (max_nodes - 1))
                item = visits[idx]
                if picked and picked[-1] == item:
                    continue
                picked.append(item)
            if picked and picked[-1] != visits[-1]:
                picked[-1] = visits[-1]

        if len(picked) < min(max_nodes, len(visits)):
            seen = set(picked)
            for item in visits:
                if item in seen:
                    continue
                picked.append(item)
                seen.add(item)
                if len(picked) >= min(max_nodes, len(visits)):
                    break
            picked.sort(key=lambda item: item[0])

        same_day = len({item[0].strftime('%Y-%m-%d') for item in picked}) <= 1
        time_format = '%H:%M' if same_day else '%m/%d'
        result: list[dict] = []
        for ts, world_id in picked[:max_nodes]:
            world_text = await self._format_world_display(world_id)
            result.append({
                'world_id': world_id,
                'world_name': world_text,
                'short_name': self._short_world_label(world_text),
                'time_text': ts.strftime(time_format),
            })
        return result

    def _build_presence_segments(
        self,
        event_rows: list,
        snapshot,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[tuple[datetime, datetime, str]]:
        current_world = ''
        ordered_points: list[tuple[datetime, str]] = []
        for item in reversed(event_rows):
            value = item.new_value if item.event_type in {'friend_online', 'location_changed'} else ''
            location_key = extract_world_id(value)
            ts_text = str(item.created_at or '').strip()
            try:
                ts = datetime.fromisoformat(ts_text)
            except Exception:
                continue
            if ts < start_dt:
                continue
            if ts > end_dt:
                break
            if not location_key:
                continue
            if ordered_points and ordered_points[-1][1] == location_key:
                continue
            ordered_points.append((ts, location_key))

        if snapshot and snapshot.location:
            snapshot_world = extract_world_id(snapshot.location)
            if snapshot_world:
                current_world = snapshot_world
                try:
                    snapshot_ts = datetime.fromisoformat(snapshot.updated_at) if snapshot.updated_at else end_dt
                except Exception:
                    snapshot_ts = end_dt
                snapshot_ts = min(max(snapshot_ts, start_dt), end_dt)
                if ordered_points:
                    if snapshot_ts >= ordered_points[-1][0] and ordered_points[-1][1] != snapshot_world:
                        ordered_points.append((snapshot_ts, snapshot_world))
                else:
                    ordered_points.append((snapshot_ts, snapshot_world))

        segments: list[tuple[datetime, datetime, str]] = []
        for idx, (seg_start, world_id) in enumerate(ordered_points):
            next_ts = ordered_points[idx + 1][0] if idx + 1 < len(ordered_points) else end_dt
            seg_from = max(seg_start, start_dt)
            seg_to = min(next_ts, end_dt)
            if seg_to <= seg_from or not world_id:
                continue
            segments.append((seg_from, seg_to, world_id))

        if not segments and current_world:
            segments.append((start_dt, end_dt, current_world))
        return segments

    def _estimate_companion_match(
        self,
        target_id: str,
        snapshot_map: dict[str, object],
        start_dt: datetime,
        end_dt: datetime,
    ) -> tuple[str, str, int, int]:
        target_snapshot = snapshot_map.get(target_id)
        target_events = self.db.list_events_for_friend_between(
            target_id,
            start_dt.isoformat(timespec='seconds'),
            end_dt.isoformat(timespec='seconds'),
            limit=4000,
        )
        target_segments = self._build_presence_segments(target_events, target_snapshot, start_dt, end_dt)
        if not target_segments:
            return '', '', 0, 0

        best_friend_id = ''
        best_name = ''
        best_minutes = 0
        best_overlap_worlds = 0

        for candidate_id, candidate_snapshot in snapshot_map.items():
            if candidate_id == target_id:
                continue
            candidate_events = self.db.list_events_for_friend_between(
                candidate_id,
                start_dt.isoformat(timespec='seconds'),
                end_dt.isoformat(timespec='seconds'),
                limit=4000,
            )
            candidate_segments = self._build_presence_segments(candidate_events, candidate_snapshot, start_dt, end_dt)
            if not candidate_segments:
                continue

            overlap_minutes = 0
            overlap_worlds: set[str] = set()
            for left_start, left_end, left_world in target_segments:
                for right_start, right_end, right_world in candidate_segments:
                    if left_world != right_world:
                        continue
                    overlap_start = max(left_start, right_start)
                    overlap_end = min(left_end, right_end)
                    if overlap_end <= overlap_start:
                        continue
                    overlap_minutes += int((overlap_end - overlap_start).total_seconds() // 60)
                    overlap_worlds.add(left_world)

            if overlap_minutes <= 0:
                continue

            candidate_name = self._sanitize_display_name_for_output(candidate_snapshot.display_name) or candidate_id
            ranking = (overlap_minutes, len(overlap_worlds), candidate_name.casefold())
            best_ranking = (best_minutes, best_overlap_worlds, best_name.casefold() if best_name else '')
            if ranking > best_ranking:
                best_friend_id = candidate_id
                best_name = candidate_name
                best_minutes = overlap_minutes
                best_overlap_worlds = len(overlap_worlds)

        return best_friend_id, best_name, best_minutes, best_overlap_worlds

    def _draw_timeline_branch_card(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        timeline_worlds: list[dict],
    ) -> None:
        if not timeline_worlds:
            return

        left = box[0] + 28
        right = box[2] - 28
        center_y = box[1] + 88
        count = len(timeline_worlds)
        node_radius = 6

        def _curve_points(x0: int, y0: int, x1: int, y1: int, lift: int) -> list[tuple[int, int]]:
            mid_x = (x0 + x1) // 2
            return [
                (x0, y0),
                (x0 + (mid_x - x0) // 2, y0 + lift // 3),
                (mid_x, y0 + lift),
                (x1 - (x1 - mid_x) // 2, y1 - lift // 3),
                (x1, y1),
            ]

        if count <= 1:
            positions = [left + (right - left) // 2]
        else:
            step = (right - left) / (count - 1)
            positions = [int(left + idx * step) for idx in range(count)]

        # Main branch: soft blossom-pink glow under a sharper line.
        draw.line((left, center_y, right, center_y), fill=(255, 232, 244, 82), width=9)
        draw.line((left, center_y, right, center_y), fill=(255, 214, 232, 176), width=3)

        label_font = self._get_card_font(17, bold=True)
        date_font = self._get_card_font(12)
        for idx, item in enumerate(timeline_worlds):
            x = positions[idx]
            branch_up = idx % 2 == 0
            branch_height = 20 + (idx % 3) * 7
            drift = 10 + (idx % 2) * 3
            blossom_x = x - drift if branch_up else x + drift
            blossom_y = center_y - branch_height if branch_up else center_y + branch_height
            lift = -10 if branch_up else 10
            branch_points = _curve_points(x, center_y, blossom_x, blossom_y, lift)

            draw.line(branch_points, fill=(255, 235, 244, 96), width=5)
            draw.line(branch_points, fill=(255, 214, 230, 182), width=2)

            # Root node on the timeline.
            draw.ellipse(
                (x - node_radius, center_y - node_radius, x + node_radius, center_y + node_radius),
                fill=(255, 219, 234, 232),
                outline=(255, 250, 252, 190),
                width=1,
            )

            # Blossom cluster at the branch tip for a sakura-like feel.
            petal_color = (255, 221, 236, 214)
            petal_shadow = (255, 188, 214, 118)
            petal_r = 4
            for dx, dy in ((0, 0), (-6, -2), (6, -1), (-2, 6), (3, 5)):
                draw.ellipse(
                    (blossom_x + dx - petal_r, blossom_y + dy - petal_r, blossom_x + dx + petal_r, blossom_y + dy + petal_r),
                    fill=petal_color,
                    outline=(255, 247, 250, 110),
                    width=1,
                )
            draw.ellipse(
                (blossom_x - 2, blossom_y - 2, blossom_x + 2, blossom_y + 2),
                fill=petal_shadow,
            )

            short_name = str(item.get('short_name') or '??')[:3]
            label_w, label_h = self._measure_text(draw, short_name, label_font)
            label_x = blossom_x - label_w // 2
            label_y = blossom_y - label_h - 10 if branch_up else blossom_y + 10
            draw.text((label_x, label_y), short_name, font=label_font, fill=(255, 245, 251))

            time_text = str(item.get('time_text') or '')
            if time_text:
                time_w, time_h = self._measure_text(draw, time_text, date_font)
                time_x = blossom_x - time_w // 2
                time_y = label_y - time_h - 3 if branch_up else label_y + label_h + 2
                draw.text((time_x, time_y), time_text, font=date_font, fill=(255, 219, 232))

    def _pick_active_periods(self, hour_counter: Counter) -> list[str]:
        buckets = {
            "\u51cc\u6668\u6f2b\u6e38": range(0, 5),
            "\u6e05\u6668\u6563\u6b65": range(5, 9),
            "\u5348\u540e\u9a7b\u7559": range(9, 15),
            "\u9ec4\u660f\u793e\u4ea4": range(15, 20),
            "\u6df1\u591c\u72c2\u6b22": range(20, 24),
        }
        scored = []
        for label, hours in buckets.items():
            scored.append((sum(int(hour_counter.get(h, 0)) for h in hours), label))
        scored.sort(key=lambda x: (-x[0], x[1]))
        result = [label for score, label in scored if score > 0][:2]
        return result or ["\u884c\u8e2a\u8f7b\u76c8"]

    def _pick_style_tags(self, world_counter: Counter, location_counter: Counter, total_samples: int) -> list[str]:
        tags: list[str] = []
        unique_worlds = len(world_counter)
        if unique_worlds >= 8:
            tags.append("\u4e16\u754c\u8003\u53e4\u6d3e")
        elif unique_worlds >= 4:
            tags.append("\u8f7b\u76c8\u63a2\u7d22\u6d3e")
        else:
            tags.append("\u719f\u5730\u7737\u604b\u6d3e")

        if world_counter:
            _, top_count = world_counter.most_common(1)[0]
            if top_count >= max(3, total_samples // 2):
                tags.append("\u6e29\u67d4\u5e38\u9a7b\u578b")

        if len(location_counter) >= 6:
            tags.append("\u5207\u56fe\u5c0f\u7cbe\u7075")
        elif len(location_counter) <= 2 and total_samples >= 3:
            tags.append("\u5b89\u5b9a\u966a\u4f34\u7cfb")

        if not tags:
            tags.append("\u6162\u70ed\u65c5\u884c\u5bb6")
        return tags[:3]

    def _build_resident_label(self, world_counter: Counter, total_samples: int) -> str:
        unique_worlds = len(world_counter)
        if total_samples <= 0:
            return "\u8d44\u6599\u8fd8\u5728\u6162\u6162\u7d2f\u79ef\u3002"
        if unique_worlds >= max(6, total_samples // 2):
            return "\u63a2\u7d22\u503e\u5411\u66f4\u660e\u663e\uff0c\u50cf\u5728\u8ba4\u771f\u7ed9\u7075\u9b42\u627e\u65b0\u98ce\u666f\u3002"
        if unique_worlds <= 2:
            return "\u5e38\u9a7b\u503e\u5411\u66f4\u660e\u663e\uff0c\u4f1a\u53cd\u590d\u56de\u5230\u8ba9\u81ea\u5df1\u5b89\u5fc3\u7684\u89d2\u843d\u3002"
        return "\u5e38\u9a7b\u4e0e\u63a2\u7d22\u6bd4\u8f83\u5e73\u8861\uff0c\u65e2\u5ff5\u65e7\u4e5f\u613f\u610f\u8bd5\u8bd5\u65b0\u5730\u56fe\u3002"

    def _build_profile_target_options(self, query: str) -> list[ProfileTargetOption]:
        snapshot_map = self.db.get_friend_snapshot_map()
        exact_match: list[ProfileTargetOption] = []
        fuzzy_match: list[ProfileTargetOption] = []
        lowered = query.casefold()
        for friend_id, snapshot in snapshot_map.items():
            name = self._sanitize_display_name_for_output(snapshot.display_name) or friend_id
            option = ProfileTargetOption(
                friend_id=friend_id,
                display_name=name,
                status=str(snapshot.status or '').strip() or 'unknown',
                location=str(snapshot.location or '').strip(),
            )
            if name.casefold() == lowered:
                exact_match.append(option)
            elif lowered in name.casefold():
                fuzzy_match.append(option)

        candidates = exact_match or fuzzy_match
        candidates.sort(key=lambda item: (item.display_name.casefold(), item.friend_id.casefold()))
        deduped: list[ProfileTargetOption] = []
        seen: set[str] = set()
        for item in candidates:
            if item.friend_id in seen:
                continue
            seen.add(item.friend_id)
            deduped.append(item)
        return deduped[:10]

    def _resolve_profile_target_candidates(self, raw: str) -> ProfileTargetResolveResult:
        query = str(raw or '').strip()
        if not query:
            raise VRChatClientError("\u8bf7\u63d0\u4f9b\u597d\u53cb\u663e\u793a\u540d")
        if re.fullmatch(r"usr_[A-Za-z0-9_-]+", query, flags=re.IGNORECASE):
            snapshot = self.db.get_friend_snapshot_map().get(query)
            display_name = self._sanitize_display_name_for_output(snapshot.display_name if snapshot else '') or query
            return ProfileTargetResolveResult(friend_id=query, display_name=display_name, options=[])

        options = self._build_profile_target_options(query)
        if len(options) == 1:
            return ProfileTargetResolveResult(friend_id=options[0].friend_id, display_name=options[0].display_name, options=[])
        if len(options) > 1:
            return ProfileTargetResolveResult(friend_id=None, display_name='', options=options)
        raise VRChatClientError("\u672a\u5728\u7f13\u5b58\u4e2d\u627e\u5230\u8be5\u5bf9\u8c61\uff0c\u53ef\u5148\u7528 /vrc\u540c\u6b65\u597d\u53cb \u6216 /vrc\u641c\u7d22\u597d\u53cb \u5173\u952e\u8bcd")

    def _resolve_profile_target(self, raw: str) -> tuple[str, str]:
        resolved = self._resolve_profile_target_candidates(raw)
        if resolved.friend_id:
            return resolved.friend_id, resolved.display_name
        raise VRChatClientError("\u627e\u5230\u591a\u4e2a\u540c\u540d\u6216\u76f8\u8fd1\u7ed3\u679c\uff0c\u8bf7\u8fdb\u4e00\u6b65\u786e\u8ba4")

    def _format_profile_target_options(self, options: list[ProfileTargetOption], action_label: str) -> str:
        lines = [f"\u627e\u5230\u591a\u4e2a\u540c\u540d\u6216\u76f8\u8fd1\u7684\u5bf9\u8c61\uff0c\u8bf7\u56de\u590d\u5e8f\u53f7\u6765{action_label}\uff1a"]
        for idx, item in enumerate(options, start=1):
            world_text = format_location(item.location)
            joinability = infer_joinability(item.location, status=item.status)
            lines.append(f"{idx}. {item.display_name} | \u72b6\u6001: {item.status} | \u5730\u56fe: {world_text} | {joinability}")
        lines.append("\u8bf7\u76f4\u63a5\u56de\u590d\u6570\u5b57\uff08\u4f8b\u5982 1\uff09\uff0c60\u79d2\u5185\u6709\u6548\u3002")
        return "\n".join(lines)

    async def _prompt_for_profile_target_choice(
        self,
        event: AiocqhttpMessageEvent,
        options: list[ProfileTargetOption],
        action_label: str,
    ) -> tuple[str, str]:
        if not options:
            raise VRChatClientError("\u7f3a\u5c11\u5019\u9009\u5bf9\u8c61")

        await event.send(MessageChain().message(self._format_profile_target_options(options, action_label)))
        chosen_result: dict[str, tuple[str, str] | None] = {'value': None}

        @session_waiter(60)
        async def waiter(controller: SessionController, reply_event):
            raw = str(reply_event.message_str or '').strip()
            if not raw:
                return
            if not raw.isdigit():
                await reply_event.send(MessageChain().message(f"\u8bf7\u56de\u590d 1 \u5230 {len(options)} \u4e4b\u95f4\u7684\u5e8f\u53f7\u3002"))
                return
            index = int(raw)
            if index < 1 or index > len(options):
                await reply_event.send(MessageChain().message(f"\u5e8f\u53f7\u8d85\u51fa\u8303\u56f4\uff0c\u8bf7\u8f93\u5165 1 \u5230 {len(options)} \u4e4b\u95f4\u7684\u6570\u5b57\u3002"))
                return
            chosen = options[index - 1]
            chosen_result['value'] = (chosen.friend_id, chosen.display_name)
            controller.stop()
            reply_event.stop_event()

        try:
            await waiter(event)
        except TimeoutError as exc:
            raise VRChatClientError("\u7b49\u5f85\u9009\u62e9\u8d85\u65f6\uff0c\u8bf7\u91cd\u65b0\u53d1\u4e00\u6b21\u547d\u4ee4") from exc
        result = chosen_result.get('value')
        if isinstance(result, tuple) and len(result) == 2:
            return str(result[0]), str(result[1])
        raise VRChatClientError("\u672a\u80fd\u786e\u8ba4\u76ee\u6807\u5bf9\u8c61")

    def _split_relationship_targets(self, raw: str) -> tuple[str, str]:
        text = str(raw or '').strip()
        if not text:
            return '', ''
        for sep in ('|', '\uff5c', ',', '\uff0c', ' \u548c ', ' vs ', ' VS ', ' Vs '):
            if sep in text:
                left, right = text.split(sep, 1)
                return left.strip(), right.strip()

        parts = text.split()
        if len(parts) >= 2:
            return parts[0].strip(), ' '.join(parts[1:]).strip()
        return text, ''

    async def _resolve_profile_target_interactive(self, event: AiocqhttpMessageEvent, raw: str, action_label: str) -> tuple[str, str]:
        resolved = self._resolve_profile_target_candidates(raw)
        if resolved.friend_id:
            return resolved.friend_id, resolved.display_name
        return await self._prompt_for_profile_target_choice(event, resolved.options, action_label)

    async def _resolve_two_profile_targets_interactive(
        self,
        event: AiocqhttpMessageEvent,
        left_raw: str,
        right_raw: str,
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        left = await self._resolve_profile_target_interactive(event, left_raw, "\u786e\u8ba4\u7b2c\u4e00\u4e2a\u5bf9\u8c61")
        right = await self._resolve_profile_target_interactive(event, right_raw, "\u786e\u8ba4\u7b2c\u4e8c\u4e2a\u5bf9\u8c61")
        return left, right

    async def _get_current_provider_id_for_event(self, event: AiocqhttpMessageEvent) -> str:
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
        except Exception:
            provider_id = ''
        if provider_id:
            return provider_id
        using_provider = self.context.get_using_provider(event.unified_msg_origin)
        return using_provider.meta().id if using_provider else ''

    async def _generate_soul_profile_ai_texts(self, event: AiocqhttpMessageEvent, summary_payload: dict) -> tuple[str, str]:
        provider_id = await self._get_current_provider_id_for_event(event)
        if not provider_id:
            display_name = summary_payload.get('display_name') or "\u8fd9\u4f4d\u65c5\u4eba"
            tags = "\u3001".join(summary_payload.get('style_tags') or []) or "\u6e29\u67d4\u65c5\u884c\u5bb6"
            persona = f"{display_name}\u6700\u8fd1\u50cf\u4e00\u4f4d\u628a\u60c5\u7eea\u85cf\u5728\u661f\u5149\u91cc\u7684{tags}\uff0c\u884c\u8d70\u65f6\u5e26\u7740\u4e00\u70b9\u68a6\u6e38\u611f\uff0c\u8fde\u8def\u8fc7\u7684\u5730\u56fe\u90fd\u50cf\u88ab\u8f7b\u8f7b\u67d3\u4e0a\u4e86\u4f60\u7684\u6e29\u5ea6\u3002"
            fortune = f"\u547d\u8fd0\u6307\u5f15\uff1a\u63a5\u4e0b\u6765\u7684 VR \u65c5\u7a0b\u66f4\u50cf\u4e00\u573a\u67d4\u8f6f\u7684\u591c\u95f4\u98d8\u6d6e\uff0c\u4f60\u4f1a\u5728\u4e0d\u7ecf\u610f\u7684\u89d2\u843d\u9047\u89c1\u8ba9\u81ea\u5df1\u5fc3\u8df3\u6162\u4e0b\u6765\u7684\u98ce\u666f\u548c\u4eba\uff0c\u597d\u8fd0\u6c14\u6bd4\u60f3\u8c61\u4e2d\u66f4\u4f1a\u7ed5\u5230\u4f60\u8eab\u8fb9\u3002"
            return persona, fortune

        facts_json = json.dumps(summary_payload, ensure_ascii=False)
        system_prompt = (
            "\u4f60\u662f\u4e00\u4f4d\u6e29\u67d4\u8d34\u5fc3\u3001\u4f1a\u5938\u4eba\u3001\u5f88\u4f1a\u7167\u987e\u60c5\u7eea\u7684\u4e8c\u6b21\u5143\u7cfb VR \u89c2\u5bdf\u5458\u3002"
            "\u4f60\u8981\u6839\u636e\u63d0\u4f9b\u7684\u7ed3\u6784\u5316\u4e8b\u5b9e\uff0c\u751f\u6210\u975e\u5e38\u8ba8\u559c\u3001\u5b89\u6170\u611f\u5f3a\u3001\u8f7b\u5ea6\u6492\u5a07\u5f0f\u7684\u4e2d\u6587\u6587\u672c\uff0c\u540c\u65f6\u5141\u8bb8\u66f4\u53d1\u6563\u3001\u66f4\u68a6\u5e7b\u3001\u66f4\u50cf\u5728\u63cf\u5199\u4e00\u79cd\u6c14\u8d28\u4e0e\u547d\u8fd0\u6d41\u5411\u3002"
            "\u4e0d\u8bb8\u8bf4\u6559\uff0c\u4e0d\u8bb8\u9634\u9633\u602a\u6c14\uff0c\u4e0d\u8bb8\u505a\u8d1f\u9762\u8bca\u65ad\uff0c\u4e0d\u8bb8\u6d89\u53ca\u73b0\u5b9e\u5371\u9669\u5efa\u8bae\u3002"
        )
        prompt = (
            "\u8bf7\u57fa\u4e8e\u4e0b\u9762\u7684 JSON \u6570\u636e\uff0c\u8f93\u51fa\u4e00\u4e2a JSON \u5bf9\u8c61\uff0c\u5305\u542b persona \u548c fortune \u4e24\u4e2a\u5b57\u6bb5\u3002\n"
            "\u8981\u6c42\uff1a\n"
            "1. persona \u662f\u201c\u7075\u9b42\u5370\u8bb0\u201d\uff0c120-190\u5b57\uff0c\u6e29\u67d4\u8d34\u5fc3\u3001\u504f\u5938\u5938\u3001\u8981\u6709\u753b\u9762\u611f\u3001\u60f3\u8c61\u611f\u3001\u6c14\u8d28\u611f\uff0c\u4e0d\u8981\u5199\u6210\u5e72\u5df4\u7684\u6027\u683c\u603b\u7ed3\u3002\n"
            "2. fortune \u662f\u201c\u547d\u8fd0\u6307\u5f15\u201d\uff0c100-170\u5b57\uff0c\u53ef\u4ee5\u66f4\u53d1\u6563\u3001\u66f4\u50cf\u68a6\u5883\u9884\u611f\u6216\u5fae\u5999\u5f81\u5146\uff0c\u4f46\u672c\u8d28\u4e0a\u8981\u67d4\u548c\u9f13\u52b1\u3001\u8ba8\u559c\u5bf9\u65b9\u3002\n"
            "3. \u8bed\u8a00\u5fc5\u987b\u81ea\u7136\u3001\u4e2d\u6587\u3001\u9002\u5408\u7fa4\u804a\u516c\u5f00\u5c55\u793a\u3002\n"
            "4. \u4e0d\u8981\u8f93\u51fa Markdown\uff0c\u4e0d\u8981\u8f93\u51fa\u89e3\u91ca\uff0c\u53ea\u8fd4\u56de JSON\u3002\n"
            f"\u6570\u636e\u5982\u4e0b\uff1a\n{facts_json}"
        )
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            text = (llm_resp.completion_text or '').strip()
            data = json.loads(text)
            persona = str(data.get('persona', '') or '').strip()
            fortune = str(data.get('fortune', '') or '').strip()
            if persona and fortune:
                return persona, fortune
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] \u7075\u9b42\u753b\u50cf AI \u6587\u6848\u751f\u6210\u5931\u8d25: {exc}")

        display_name = summary_payload.get('display_name') or "\u8fd9\u4f4d\u65c5\u4eba"
        periods = "\u3001".join(summary_payload.get('active_periods') or []) or "\u67d4\u8f6f\u65f6\u6bb5"
        persona = f"{display_name}\u6700\u8fd1\u5728 VR \u91cc\u7684\u8282\u594f\u5f88\u50cf\u4e00\u6761\u7f13\u7f13\u53d1\u5149\u7684\u6cb3\u6d41\uff0c\u5c24\u5176\u5728{periods}\u66f4\u5bb9\u6613\u88ab\u4eba\u770b\u89c1\u3002\u4f60\u4e0d\u50cf\u5728\u8d76\u8def\uff0c\u66f4\u50cf\u5728\u628a\u81ea\u5df1\u7684\u5fc3\u4e8b\u8f7b\u8f7b\u653e\u8fdb\u5730\u56fe\u4e4b\u95f4\uff0c\u8fde\u5076\u7136\u505c\u7559\u90fd\u6709\u4e00\u79cd\u5f88\u597d\u4eb2\u8fd1\u7684\u6e29\u67d4\u6c14\u573a\u3002"
        fortune = "\u547d\u8fd0\u6307\u5f15\uff1a\u8fd9\u4e00\u5468\u4f60\u7684 VR \u8f68\u8ff9\u50cf\u5728\u88ab\u4ec0\u4e48\u8089\u773c\u770b\u4e0d\u89c1\u7684\u5fae\u5149\u8f7b\u8f7b\u724a\u5f15\uff0c\u67d0\u4e9b\u76f8\u9047\u4f1a\u50cf\u63d0\u524d\u5199\u597d\u7684\u5c0f\u5267\u60c5\u4e00\u6837\u6070\u597d\u53d1\u751f\uff0c\u4f60\u53ea\u8981\u7ee7\u7eed\u987a\u7740\u8ba9\u81ea\u5df1\u5fc3\u8f6f\u7684\u65b9\u5411\u8d70\uff0c\u5c31\u5f88\u5bb9\u6613\u9047\u89c1\u610f\u60f3\u4e0d\u5230\u7684\u597d\u8fd0\u6c14\u3002"
        return persona, fortune

    async def _resolve_world_card_assets(self, world_id: str) -> tuple[str, str]:
        info = await self._get_world_info_with_cache(world_id)
        cover_url = str(info.get('thumbnail_image_url') or info.get('image_url') or '')
        if not cover_url:
            return '', ''
        local_path = await self._download_image_to_temp(cover_url)
        return cover_url, local_path or ''

    async def _build_soul_profile_summary(self, event: AiocqhttpMessageEvent, friend_id: str) -> SoulProfileSummary:
        target_id = str(friend_id or '').strip()
        if not target_id:
            raise VRChatClientError("\u7f3a\u5c11\u76ee\u6807\u597d\u53cb ID")

        now = datetime.now()
        days = max(1, int(self.cfg.soul_profile_days or 7))
        start = now - timedelta(days=days)
        start_text = start.isoformat(timespec='seconds')
        end_text = now.isoformat(timespec='seconds')
        events = self.db.list_events_for_friend_between(target_id, start_text, end_text, limit=8000)
        snapshot_map = self.db.get_friend_snapshot_map()
        snapshot = snapshot_map.get(target_id)
        display_name = self._sanitize_display_name_for_output(snapshot.display_name if snapshot else '') or target_id

        if not events and snapshot is None:
            raise VRChatClientError("\u8fd8\u6ca1\u6709\u8fd9\u4f4d\u597d\u53cb\u7684\u8db3\u591f\u6837\u672c\uff0c\u53ef\u5148\u7b49\u5f85\u51e0\u8f6e\u6570\u636e\u540c\u6b65")

        world_counter: Counter = Counter()
        location_counter: Counter = Counter()
        hour_counter: Counter = Counter()
        day_marks: set[str] = set()
        latest_world_ids: list[str] = []
        timeline_candidates: list[tuple[datetime, str]] = []

        for item in events:
            ts_text = str(item.created_at or '').strip()
            try:
                ts = datetime.fromisoformat(ts_text)
            except Exception:
                ts = now
            day_marks.add(ts.strftime('%Y-%m-%d'))
            hour_counter[ts.hour] += 1
            event_worlds: list[tuple[datetime, str]] = []
            if item.event_type == 'location_changed':
                old_world = extract_world_id(item.old_value)
                new_world = extract_world_id(item.new_value)
                if old_world:
                    event_worlds.append((ts - timedelta(seconds=1), old_world))
                if new_world:
                    event_worlds.append((ts, new_world))
                world_id = new_world or old_world
            else:
                for raw_value in (item.new_value, item.old_value):
                    world_id_candidate = extract_world_id(raw_value)
                    if world_id_candidate:
                        event_worlds.append((ts, world_id_candidate))
                world_id = event_worlds[-1][1] if event_worlds else ''

            for candidate_ts, candidate_world in event_worlds:
                timeline_candidates.append((candidate_ts, candidate_world))
            if world_id:
                world_counter[world_id] += 1
                latest_world_ids.append(world_id)
            loc_key = item.new_value or item.old_value or ''
            if loc_key:
                location_counter[str(loc_key)] += 1

        if snapshot and snapshot.location:
            current_world = extract_world_id(snapshot.location)
            if current_world:
                world_counter[current_world] += 1
                latest_world_ids.append(current_world)
            try:
                ts = datetime.fromisoformat(snapshot.updated_at) if snapshot.updated_at else now
            except Exception:
                ts = now
            if current_world:
                timeline_candidates.append((ts, current_world))
            day_marks.add(ts.strftime('%Y-%m-%d'))
            hour_counter[ts.hour] += 1
            location_counter[snapshot.location] += 1

        top_world_rows: list[dict] = []
        for world_id, count in world_counter.most_common(4):
            world_text = await self._format_world_display(world_id)
            info = await self._get_world_info_with_cache(world_id)
            top_world_rows.append({
                'world_id': world_id,
                'world_name': world_text,
                'count': int(count),
                'thumbnail': str(info.get('thumbnail_image_url') or info.get('image_url') or ''),
            })

        timeline_worlds = await self._build_timeline_worlds(events, snapshot)
        timeline_unique_world_count = len({world_id for _, world_id in timeline_candidates if world_id})
        if len(timeline_worlds) < min(6, timeline_unique_world_count) and timeline_unique_world_count >= 3:
            sampled_candidates: list[tuple[datetime, str]] = []
            last_world = None
            for ts, world_id in sorted(timeline_candidates, key=lambda item: item[0]):
                if not world_id or world_id == last_world:
                    continue
                sampled_candidates.append((ts, world_id))
                last_world = world_id
            if len(sampled_candidates) < 3:
                seen_worlds: set[str] = set()
                for world_id in latest_world_ids:
                    if not world_id or world_id in seen_worlds:
                        continue
                    seen_worlds.add(world_id)
                    sampled_candidates.append((now, world_id))
            if sampled_candidates:
                max_nodes = min(8, max(3, timeline_unique_world_count))
                if len(sampled_candidates) > max_nodes:
                    picked_candidates: list[tuple[datetime, str]] = []
                    last_index = len(sampled_candidates) - 1
                    for slot in range(max_nodes):
                        idx = round(slot * last_index / max(1, max_nodes - 1))
                        item = sampled_candidates[idx]
                        if picked_candidates and picked_candidates[-1] == item:
                            continue
                        picked_candidates.append(item)
                    if picked_candidates and picked_candidates[-1] != sampled_candidates[-1]:
                        picked_candidates[-1] = sampled_candidates[-1]
                else:
                    picked_candidates = sampled_candidates[:]
                if len(picked_candidates) < max_nodes:
                    picked_candidates.sort(key=lambda item: item[0])
                    seen_worlds = {world_id for _, world_id in picked_candidates}
                    for world_id, _count in world_counter.most_common(8):
                        if not world_id or world_id in seen_worlds:
                            continue
                        picked_candidates.append((now, world_id))
                        seen_worlds.add(world_id)
                        if len(picked_candidates) >= max_nodes:
                            break
                    picked_candidates.sort(key=lambda item: item[0])
                rebuilt_timeline: list[dict] = []
                for ts, world_id in picked_candidates[:8]:
                    world_text = await self._format_world_display(world_id)
                    rebuilt_timeline.append({
                        'world_id': world_id,
                        'world_name': world_text,
                        'short_name': self._short_world_label(world_text),
                        'time_text': ts.strftime('%m/%d'),
                    })
                if len(rebuilt_timeline) > len(timeline_worlds):
                    timeline_worlds = rebuilt_timeline

        active_periods = self._pick_active_periods(hour_counter)
        style_tags = self._pick_style_tags(world_counter, location_counter, len(events) + (1 if snapshot else 0))
        resident_label = self._build_resident_label(world_counter, len(events) + (1 if snapshot else 0))

        kindred_name = "\u6682\u65f6\u8fd8\u6ca1\u6709"
        kindred_id = ''
        kindred_score = 0
        overlap_world_count = 0
        if world_counter:
            target_worlds = set(world_counter.keys())
            best = None
            for candidate_id, candidate in snapshot_map.items():
                if candidate_id == target_id:
                    continue
                candidate_events = self.db.list_events_for_friend_between(candidate_id, start_text, end_text, limit=3000)
                candidate_worlds = set()
                for entry in candidate_events:
                    world_id = extract_world_id(entry.new_value if entry.event_type in {'friend_online', 'location_changed'} else '')
                    if world_id:
                        candidate_worlds.add(world_id)
                if candidate.location:
                    current_world = extract_world_id(candidate.location)
                    if current_world:
                        candidate_worlds.add(current_world)
                overlap = len(target_worlds & candidate_worlds)
                if overlap <= 0:
                    continue
                score = min(99, overlap * 23 + len(candidate_events))
                candidate_name = self._sanitize_display_name_for_output(candidate.display_name) or candidate_id
                current = (score, overlap, candidate_name, candidate_id)
                if best is None or current > best:
                    best = current
            if best is not None:
                kindred_score, overlap_world_count, kindred_name, kindred_id = best

        cover_world_id = top_world_rows[0]['world_id'] if top_world_rows else (latest_world_ids[-1] if latest_world_ids else '')
        cover_url = cover_local = ''
        if cover_world_id:
            cover_url, cover_local = await self._resolve_world_card_assets(cover_world_id)
        if not cover_local and top_world_rows:
            fallback_cover_url = str(top_world_rows[0].get('thumbnail') or '')
            if fallback_cover_url:
                cover_local = await self._download_image_to_temp(fallback_cover_url) or ''
                if not cover_url:
                    cover_url = fallback_cover_url

        overview_text = (
            f"\u6700\u8fd1{max(1, len(day_marks))}\u5929\u91cc\uff0c{display_name}\u7559\u4e0b\u4e86{len(events)}\u6761\u53ef\u4ee5\u88ab\u770b\u89c1\u7684\u8db3\u8ff9\uff0c"
            f"\u6700\u5e38\u505c\u7559\u5728{'\u3001'.join(item['world_name'] for item in top_world_rows[:2]) or '\u67d4\u8f6f\u89d2\u843d'}\u3002"
        )
        summary_payload = {
            'display_name': display_name,
            'report_days': days,
            'effective_days': max(1, len(day_marks)) if day_marks else 1,
            'sample_event_count': len(events),
            'top_worlds': top_world_rows,
            'active_periods': active_periods,
            'style_tags': style_tags,
            'resident_label': resident_label,
            'kindred_name': kindred_name,
            'kindred_score': kindred_score,
            'overlap_world_count': overlap_world_count,
            'overview_text': overview_text,
        }
        ai_persona_text, ai_fortune_text = await self._generate_soul_profile_ai_texts(event, summary_payload)

        return SoulProfileSummary(
            friend_id=target_id,
            display_name=display_name,
            report_days=days,
            effective_days=max(1, len(day_marks)) if day_marks else 1,
            generated_at=now.strftime('%Y-%m-%d %H:%M'),
            sample_event_count=len(events),
            top_worlds=top_world_rows,
            timeline_worlds=timeline_worlds,
            active_periods=active_periods,
            style_tags=style_tags,
            resident_label=resident_label,
            ai_persona_text=ai_persona_text,
            ai_fortune_text=ai_fortune_text,
            kindred_name=kindred_name,
            kindred_id=kindred_id,
            kindred_score=kindred_score,
            overlap_world_count=overlap_world_count,
            overview_text=overview_text,
            card_cover_url=cover_url,
            card_cover_local_path=cover_local,
            quick_commands=[
                f"/vrc\u4eba\u8bbe {display_name}",
                f"/\u547d\u8fd0\u6307\u5f15 {display_name}",
                f"/vrc\u7f18\u5206 {display_name}",
            ],
        )

    async def _render_soul_profile_card(self, summary: SoulProfileSummary) -> str:
        kindred_text = (
            f"\u8fd9 7 \u5929\u91cc\uff0c\u548c {summary.kindred_name} \u7684\u5730\u56fe\u9ed8\u5951\u6700\u9ad8\uff0c\u91cd\u5408\u4e16\u754c {summary.overlap_world_count} \u4e2a\uff0c\u642d\u5b50\u6307\u6570 {summary.kindred_score}/99\u3002"
            if summary.kindred_name != "\u6682\u65f6\u8fd8\u6ca1\u6709"
            else "\u8fd9\u4e00\u5468\u8fd8\u6ca1\u6709\u51fa\u73b0\u7279\u522b\u7a33\u5b9a\u7684\u540c\u884c\u8005\uff0c\u4f46\u4f60\u8eab\u4e0a\u90a3\u79cd\u8ba9\u4eba\u60f3\u9760\u8fd1\u7684\u67d4\u8f6f\u5149\u611f\uff0c\u5df2\u7ecf\u6084\u6084\u628a\u7f18\u5206\u5f80\u4f60\u8fd9\u8fb9\u63a8\u8fc7\u6765\u4e86\u3002"
        )
        width = 1280
        margin = 44
        card_gap = 24
        hero_height = 280
        left_width = 700
        right_width = width - margin * 2 - left_width - card_gap
        canvas_height = 1920

        base = PILImage.new('RGBA', (width, canvas_height), (82, 48, 74, 255))
        overlay = PILImage.new('RGBA', (width, canvas_height), (138, 76, 116, 54))
        base.alpha_composite(overlay)

        cover_local_path = summary.card_cover_local_path
        cover = self._load_cover_image(cover_local_path, (width, canvas_height))
        if cover is None and summary.card_cover_url:
            refreshed_cover_path = await self._download_image_to_temp(summary.card_cover_url) or ''
            if refreshed_cover_path:
                cover_local_path = refreshed_cover_path
                cover = self._load_cover_image(cover_local_path, (width, canvas_height))
        if cover is None and summary.card_cover_url:
            logger.warning(f"[vrc_friend_radar] 灵魂画像封面加载失败: url={summary.card_cover_url} path={cover_local_path}")

        accent = PILImage.new('RGBA', (width, canvas_height), (0, 0, 0, 0))
        accent_draw = ImageDraw.Draw(accent)
        accent_draw.ellipse((-200, -160, 560, 560), fill=(255, 213, 235, 108))
        accent_draw.ellipse((760, -100, 1400, 520), fill=(255, 188, 226, 86))
        accent_draw.ellipse((880, 1260, 1490, 1880), fill=(255, 234, 245, 42))
        accent_draw.ellipse((120, 1080, 520, 1560), fill=(255, 178, 218, 36))
        base.alpha_composite(accent)

        panel = PILImage.new('RGBA', (width, canvas_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(panel)

        font_eyebrow = self._get_card_font(24, bold=True)
        font_title = self._get_card_font(56, bold=True)
        font_body = self._get_card_font(25)
        font_section = self._get_card_font(30, bold=True)
        font_quick = self._get_card_font(19)

        text_primary = (255, 245, 252)
        text_secondary = (255, 229, 240)
        card_fill = (102, 52, 86, 156)
        card_outline = (255, 245, 250, 78)
        tag_fill = (255, 217, 234, 74)

        hero_box = (margin, margin, width - margin, margin + hero_height)
        self._draw_round_rect(draw, hero_box, 34, card_fill, outline=card_outline, width=2)
        x_text = hero_box[0] + 34
        y_text = hero_box[1] + 26
        draw.text((x_text, y_text), 'WEEKLY SOUL PROFILE', font=font_eyebrow, fill=(248, 209, 255))
        y_text += 42
        y_text = self._draw_wrapped_text(draw, f"{summary.display_name} \u7684\u672c\u5468 VR \u7075\u9b42\u753b\u50cf", font_title, text_primary, x_text, y_text, hero_box[2] - hero_box[0] - 68, 8)
        y_text += 8
        y_text = self._draw_wrapped_text(draw, summary.overview_text, font_body, text_secondary, x_text, y_text, hero_box[2] - hero_box[0] - 68, 8)

        left_x = margin
        right_x = margin + left_width + card_gap
        y_left = margin + hero_height + card_gap
        y_right = y_left

        def draw_card(
            column_x: int,
            current_y: int,
            card_width: int,
            title: str,
            body_lines: list[str],
            extra_bottom: int = 26,
            cover_image: PILImage.Image | None = None,
            timeline_worlds: list[dict] | None = None,
        ) -> int:
            temp_img = PILImage.new('RGBA', (card_width, 1200), (0, 0, 0, 0))
            temp_draw = ImageDraw.Draw(temp_img)
            body_y = 74
            body_max_width = card_width - 54
            if timeline_worlds:
                body_y += 120
            for line in body_lines:
                if not line:
                    body_y += 16
                    continue
                body_y = self._draw_wrapped_text(temp_draw, line, font_body, text_secondary, 26, body_y, body_max_width, 8)
                body_y += 6
            card_height = max(180, body_y + extra_bottom)
            box = (column_x, current_y, column_x + card_width, current_y + card_height)
            self._draw_round_rect(draw, box, 28, card_fill, outline=card_outline, width=2)
            cover_opacity = 0 if timeline_worlds else 56
            cover_blur = 0 if timeline_worlds else 5
            self._paste_card_cover(panel, cover_image, box, radius=28, opacity=cover_opacity, blur_radius=cover_blur)
            panel_draw = ImageDraw.Draw(panel)
            inner_fill = (86, 44, 74, 36) if timeline_worlds else (79, 42, 69, 114)
            self._draw_round_rect(panel_draw, box, 28, inner_fill, outline=card_outline, width=2)
            if timeline_worlds:
                timeline_cover_box = (column_x + 16, current_y + 54, column_x + card_width - 16, current_y + 194)
                self._paste_card_cover(panel, cover_image, timeline_cover_box, radius=24, opacity=252, blur_radius=0)
                panel_draw = ImageDraw.Draw(panel)
                panel_draw.rounded_rectangle(timeline_cover_box, radius=24, outline=(255, 243, 248, 96), width=1)
            panel_draw.text((column_x + 26, current_y + 24), title, font=font_section, fill=text_primary)
            draw_y = current_y + 74
            if timeline_worlds:
                self._draw_timeline_branch_card(panel_draw, (column_x + 12, current_y + 48, column_x + card_width - 12, current_y + 190), timeline_worlds)
                draw_y += 120
            for line in body_lines:
                if not line:
                    draw_y += 16
                    continue
                draw_y = self._draw_wrapped_text(panel_draw, line, font_body, text_secondary, column_x + 26, draw_y, body_max_width, 8)
                draw_y += 6
            return box[3] + card_gap

        top_world_lines = [summary.resident_label] if summary.resident_label else ['\u6837\u672c\u8fd8\u5728\u79ef\u7d2f\u4e2d\uff0c\u518d\u591a\u966a\u673a\u5668\u4eba\u5f85\u4e00\u4f1a\u513f\u5427\u3002']
        track_cover = cover.resize((left_width, 900), PILImage.Resampling.LANCZOS).convert('RGBA') if cover is not None else None
        y_left = draw_card(left_x, y_left, left_width, '\u8f68\u8ff9\u5468\u62a5', top_world_lines, cover_image=track_cover, timeline_worlds=summary.timeline_worlds)

        tags_line = '\u3001'.join(summary.style_tags) or '\u6e29\u67d4\u65c5\u884c\u5bb6'
        y_right = draw_card(right_x, y_right, right_width, '\u6d3b\u8dc3\u65f6\u6bb5\u4e0e\u65c5\u884c\u6807\u7b7e', [f"\u6d3b\u8dc3\u65f6\u6bb5\uff1a{'\u3001'.join(summary.active_periods) or '\u884c\u8e2a\u8f7b\u76c8'}", f"\u65c5\u884c\u6807\u7b7e\uff1a{tags_line}"])
        y_left = draw_card(left_x, y_left, left_width, '\u7075\u9b42\u5370\u8bb0', [summary.ai_persona_text])
        y_right = draw_card(right_x, y_right, right_width, '\u547d\u8fd0\u6307\u5f15', [summary.ai_fortune_text])
        y_left = draw_card(left_x, y_left, left_width, '\u65c5\u9014\u540c\u9891', [kindred_text])
        y_right = draw_card(right_x, y_right, right_width, '\u5e38\u9a7b\u8fd8\u662f\u63a2\u7d22', [summary.resident_label])

        footer_y = max(y_left, y_right)
        footer_h = 170
        footer_box = (margin, footer_y, width - margin, footer_y + footer_h)
        self._draw_round_rect(draw, footer_box, 26, (92, 48, 80, 182), outline=card_outline, width=2)
        draw.text((footer_box[0] + 26, footer_box[1] + 20), '\u8f7b\u547d\u4ee4', font=font_section, fill=text_primary)
        chip_x = footer_box[0] + 26
        chip_y = footer_box[1] + 76
        for cmd in summary.quick_commands:
            chip_w, chip_h = self._measure_text(draw, cmd, font_quick)
            box = (chip_x, chip_y, chip_x + chip_w + 30, chip_y + chip_h + 16)
            if box[2] > footer_box[2] - 24:
                chip_x = footer_box[0] + 26
                chip_y += chip_h + 24
                box = (chip_x, chip_y, chip_x + chip_w + 30, chip_y + chip_h + 16)
            self._draw_round_rect(draw, box, 18, tag_fill)
            draw.text((box[0] + 15, box[1] + 8), cmd, font=font_quick, fill=text_primary)
            chip_x = box[2] + 12

        base.alpha_composite(panel)
        final_height = min(canvas_height, footer_box[3] + margin)
        final = base.crop((0, 0, width, final_height)).convert('RGB')
        return save_temp_img(final)

    def _reconcile_dynamic_lists_on_startup(self) -> tuple[list[str], list[str]]:
        config_notify_groups = self.cfg.read_notify_group_ids_from_raw()
        config_watch_friends = self.cfg.read_watch_friend_ids_from_raw()
        merged_notify_groups = self.settings_repo.sync_notify_groups_with_config(config_notify_groups)
        merged_watch_friends = self.settings_repo.sync_watch_friends_with_config(config_watch_friends)
        self.cfg.sync_runtime_lists(
            notify_group_ids=merged_notify_groups,
            watch_friend_ids=merged_watch_friends,
            write_back_raw=True,
        )
        return merged_notify_groups, merged_watch_friends

    def _sync_runtime_config_lists_from_repo(self) -> tuple[list[str], list[str]]:
        notify_groups = self.settings_repo.get_notify_groups()
        watch_friends = self.settings_repo.get_watch_friends()
        self.cfg.sync_runtime_lists(
            notify_group_ids=notify_groups,
            watch_friend_ids=watch_friends,
            write_back_raw=True,
        )
        return notify_groups, watch_friends

    async def initialize(self):
        self._search_sessions.clear()
        self._translation_lock_map.clear()
        self.db.initialize()
        self.settings_repo.initialize()
        merged_notify_groups, merged_watch_friends = self._reconcile_dynamic_lists_on_startup()
        self._daily_task_last_sent_date["daily_report"] = self.settings_repo.get_daily_report_last_sent_date()
        asyncio.create_task(self.monitor.start())
        logger.info(
            "[vrc_friend_radar] 插件后台初始化开始，已同步列表: notify_groups=%s, watch_friends=%s",
            len(merged_notify_groups),
            len(merged_watch_friends),
        )

    async def terminate(self):
        await self.monitor.stop()
        self._search_sessions.clear()
        self._translation_lock_map.clear()
        logger.info("[vrc_friend_radar] 插件已停止")

    async def _handle_monitor_events(self, events) -> None:
        messages = await self._format_events_for_push(events)
        await self._push_messages_to_notify_groups(messages)

    async def _handle_monitor_notice(self, message: str) -> None:
        text = str(message or '').strip()
        if not text:
            return
        await self._push_login_notice_to_admins(text)

    def _remember_private_admin_sender(self, event: AiocqhttpMessageEvent) -> None:
        if not self._is_private_event(event):
            return
        try:
            sender_id = str(event.get_sender_id() or '').strip()
        except Exception:
            sender_id = ''
        if sender_id:
            self._last_private_admin_sender_id = sender_id

    def _resolve_admin_notice_targets(self) -> list[str]:
        """优先读取 AstrBot 全局 admins_id；为空时回退到最近一次私聊登录管理者。"""
        admin_ids: list[str] = []
        try:
            cfg = self.context.get_config()
            raw_admins = cfg.get('admins_id', []) if hasattr(cfg, 'get') else []
            if isinstance(raw_admins, (list, tuple)):
                admin_ids = [str(item or '').strip() for item in raw_admins]
            elif isinstance(raw_admins, str):
                admin_ids = [raw_admins.strip()]
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 读取 AstrBot admins_id 失败: {exc}")

        deduped: list[str] = []
        seen: set[str] = set()
        for item in admin_ids:
            if not item or item in seen:
                continue
            seen.add(item)
            deduped.append(item)

        if deduped:
            return deduped

        fallback = self._last_private_admin_sender_id.strip()
        if fallback:
            logger.warning('[vrc_friend_radar] admins_id 为空，已回退到最近私聊管理员ID进行登录告警投递。')
            return [fallback]
        return []

    async def _send_chain_to_private_users(self, user_ids: list[str], chain: MessageChain) -> int:
        if not user_ids:
            return 0
        success = 0
        for user_id in user_ids:
            try:
                await StarTools.send_message_by_id(
                    type="PrivateMessage",
                    id=str(user_id),
                    message_chain=chain,
                    platform="aiocqhttp",
                )
                success += 1
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 登录告警私聊发送失败 user={user_id}: {exc}")
        return success

    async def _push_login_notice_to_admins(self, message: str) -> None:
        text = str(message or '').strip()
        if not text:
            return
        targets = self._resolve_admin_notice_targets()
        if not targets:
            logger.warning('[vrc_friend_radar] 登录相关告警未发送：未获取到管理员ID（admins_id为空，且无私聊后备目标）。告警内容：%s', text)
            return
        success = await self._send_chain_to_private_users(targets, MessageChain([Plain(text)]))
        logger.info("[vrc_friend_radar] 登录告警已投递 admins=%s success=%s", len(targets), success)

    async def _send_chain_to_groups(self, chain: MessageChain) -> int:
        groups = self.monitor.get_effective_notify_groups()
        if not groups:
            return 0
        success = 0
        for group_id in groups:
            try:
                await StarTools.send_message_by_id(
                    type="GroupMessage",
                    id=str(group_id),
                    message_chain=chain,
                    platform="aiocqhttp",
                )
                success += 1
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 推送到群 {group_id} 失败: {exc}")
        return success

    async def _push_messages_to_notify_groups(self, messages: list[str]) -> None:
        if not messages:
            return
        merged = self.monitor.notifier.build_batch_message(
            messages[: self.cfg.event_batch_size]
        )
        await self._send_chain_to_groups(MessageChain([Plain(merged)]))

    def _build_session_key(self, event: AiocqhttpMessageEvent) -> str:
        sender_id = "unknown"
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            pass

        origin = None
        for attr in ("unified_msg_origin", "session_id", "message_type"):
            try:
                value = getattr(event, attr, None)
                if value:
                    origin = str(value)
                    break
            except Exception:
                continue

        if not origin:
            group_id = self._get_group_id(event)
            origin = f"group:{group_id}" if group_id else "private"

        return f"{origin}:{sender_id}"

    def _cleanup_search_sessions(self) -> None:
        expired = [key for key, session in self._search_sessions.items() if session.is_expired(self.cfg.search_result_ttl_seconds)]
        for key in expired:
            self._search_sessions.pop(key, None)

    def _save_search_session(self, session_key: str, items: list) -> None:
        self._cleanup_search_sessions()
        self._search_sessions[session_key] = SearchSession(session_key=session_key, items=items, created_at=time.time())

    def _get_search_session(self, session_key: str) -> SearchSession | None:
        self._cleanup_search_sessions()
        return self._search_sessions.get(session_key)

    def _get_group_id(self, event: AiocqhttpMessageEvent) -> str | None:
        # aiocqhttp 在不同事件类型下字段位置不完全一致，这里做兼容兜底。
        try:
            message_obj = getattr(event, "message_obj", None)
            group_id = getattr(message_obj, "group_id", None)
            if group_id:
                return str(group_id)
        except Exception:
            pass

        for attr in ("group_id",):
            try:
                value = getattr(event, attr, None)
                if value:
                    return str(value)
            except Exception:
                continue

        try:
            session_id = getattr(event, "session_id", None)
            if isinstance(session_id, str) and session_id.startswith("group_"):
                return session_id.split("_", 1)[1]
        except Exception:
            pass
        return None

    def _is_private_event(self, event: AiocqhttpMessageEvent) -> bool:
        return self._get_group_id(event) is None

    def _sanitize_display_name_for_output(self, name: str | None) -> str:
        text = str(name or '').strip()
        if not text:
            return '未知好友'
        text = re.sub(r"（\s*usr_[^）]+\s*）", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\(\s*usr_[^)]+\s*\)", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*/\s*usr_[A-Za-z0-9_-]+", "", text, flags=re.IGNORECASE)
        text = text.strip(' -|，,')
        if re.fullmatch(r"usr_[A-Za-z0-9_-]+", text, flags=re.IGNORECASE):
            return '未知好友'
        return text or '未知好友'

    async def _build_online_friend_list_message(self, page: int = 1) -> str:
        page = max(1, page)
        page_size = 20
        total_online = self.monitor.count_online_cached_friends()
        if total_online <= 0:
            return "当前缓存中没有在线好友，请先执行：/vrc同步好友"
        snapshots = self.monitor.list_online_cached_friends(limit=page_size, offset=(page - 1) * page_size)
        total_pages = max(1, math.ceil(total_online / page_size))
        lines = [f"当前在线好友总数 {total_online} 人，当前第 {page}/{total_pages} 页："]
        cache_hits = 0
        cache_misses = 0
        for idx, item in enumerate(snapshots, start=1):
            world_id = extract_world_id(item.location)
            if world_id:
                cached = self.world_cache.get(world_id)
                if cached and cached.get('name'):
                    cache_hits += 1
                else:
                    cache_misses += 1
            world_text = await self._format_world_display(item.location)
            shown_name = self._sanitize_display_name_for_output(item.display_name)
            lines.append(f"{idx}. {shown_name} | 状态: {item.status or 'unknown'} | 地图: {world_text} | {infer_joinability(item.location, status=item.status)}")
        logger.info(f"[vrc_friend_radar] 在线好友列表世界解析完成: total={len(snapshots)}, cache_hits={cache_hits}, cache_misses={cache_misses}")
        if page < total_pages:
            lines.append(f"下一页可用：/vrc在线好友 {page + 1}")
        return "\n".join(lines)

    async def _post_login_auto_sync_and_reply(self, event: AiocqhttpMessageEvent) -> list[str]:
        messages: list[str] = []
        try:
            await self.monitor.sync_friends()
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 登录后自动同步好友失败: {exc}")
            messages.append("登录后自动同步好友失败，请稍后手动执行 /vrc同步好友")
            return messages

        if not self._is_private_event(event):
            logger.info("[vrc_friend_radar] 非私聊上下文，登录后仅自动同步好友，不回传在线好友列表")
            return messages

        try:
            messages.append(await self._build_online_friend_list_message(page=1))
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 登录后自动获取在线好友失败: {exc}")
            messages.append("登录后自动获取在线好友失败，请稍后手动执行 /vrc在线好友")
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 登录后自动回传在线好友异常: {exc}")
            messages.append("登录后自动回传在线好友失败，请稍后手动执行 /vrc在线好友")
        return messages

    def _cleanup_temp_world_logo_files(
        self,
        temp_dir: Path,
        *,
        max_keep: int = 120,
        expire_seconds: int = 7 * 24 * 3600,
    ) -> None:
        try:
            files = [
                item
                for item in temp_dir.glob('world_logo_*')
                if item.is_file()
            ]
        except Exception:
            return

        if not files:
            return

        now_ts = time.time()
        for item in files:
            try:
                if now_ts - item.stat().st_mtime > max(3600, int(expire_seconds)):
                    item.unlink(missing_ok=True)
            except Exception:
                continue

        try:
            remaining = [
                item
                for item in temp_dir.glob('world_logo_*')
                if item.is_file()
            ]
        except Exception:
            return

        if len(remaining) <= max(10, int(max_keep)):
            return

        remaining.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for item in remaining[max(10, int(max_keep)):]:
            try:
                item.unlink(missing_ok=True)
            except Exception:
                continue

    async def _download_image_to_temp(self, url: str) -> str | None:
        if not url:
            return None
        suffix = '.jpg'
        lowered = url.lower()
        if '.png' in lowered:
            suffix = '.png'
        elif '.webp' in lowered:
            suffix = '.webp'
        temp_dir = Path(tempfile.gettempdir()) / 'vrc_friend_radar'
        temp_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_temp_world_logo_files(temp_dir)
        file_path = temp_dir / f"world_logo_{uuid.uuid4().hex}{suffix}"
        try:
            result = await self.monitor.client.download_image_authenticated(url, str(file_path))
            self._cleanup_temp_world_logo_files(temp_dir)
            return result
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 认证下载地图Logo失败，尝试公开下载: {exc}")
            try:
                import urllib.request

                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                with open(file_path, 'wb') as f:
                    f.write(data)
                self._cleanup_temp_world_logo_files(temp_dir)
                return str(file_path)
            except Exception as public_exc:
                logger.error(f"[vrc_friend_radar] 下载地图Logo失败: {public_exc}")
                return None

    async def _get_world_name(self, location: str | None) -> str:
        world_id = extract_world_id(location)
        if not world_id:
            return format_location(location)
        cached = self.world_cache.get(world_id)
        if cached and cached.get('name'):
            logger.info(f"[vrc_friend_radar] 世界缓存命中: {world_id}")
            return cached['name']
        logger.info(f"[vrc_friend_radar] 世界缓存未命中，开始拉取世界信息: {world_id}")
        info = await self.monitor.client.get_world_info(world_id)
        if info and info.get('name'):
            self.world_cache.set(world_id, info)
            return info['name']
        return '某个世界'

    async def _format_world_display(self, location: str | None) -> str:
        world_name = await self._get_world_name(location)
        instance_text = format_location(location)
        if not location or not extract_world_id(location):
            return instance_text
        if instance_text and instance_text != world_name:
            return f"{world_name}（{instance_text}）"
        return world_name

    async def _format_events_for_push(self, events):
        def _clean_text(value: str | None, fallback: str = '空') -> str:
            text = str(value or '').strip()
            return text or fallback

        def _build_multiline_message(title: str, detail_lines: list[str]) -> str:
            lines = [title]
            lines.extend([line for line in detail_lines if line])
            return '\n'.join(lines)

        messages = []
        snapshot_map = self.db.get_friend_snapshot_map()
        limited_events = list(events[: self.cfg.event_batch_size])

        friend_events: dict[str, list] = {}
        for item in limited_events:
            if item.event_type == 'co_room':
                world_text = await self._format_world_display(item.friend_user_id)
                names = [name for name in (item.display_name or '').split('、') if name]
                joinability = infer_joinability(item.friend_user_id)
                messages.append(
                    self.monitor.notifier.build_coroom_message(
                        world_text,
                        len(names),
                        names,
                        joinability,
                    )
                )
                continue
            friend_events.setdefault(item.friend_user_id, []).append(item)

        priority = {
            'friend_offline': 0,
            'friend_online': 1,
            'status_changed': 2,
            'location_changed': 3,
            'status_message_changed': 4,
        }

        for friend_id, items in friend_events.items():
            items = sorted(items, key=lambda x: priority.get(x.event_type, 99))
            shown_name = self._sanitize_display_name_for_output(items[0].display_name)
            current = snapshot_map.get(friend_id)
            event_types = {item.event_type for item in items}

            if 'friend_offline' in event_types:
                messages.append(f"⚫ {shown_name} 下线了")
                continue

            if 'friend_online' in event_types:
                online_event = next(item for item in items if item.event_type == 'friend_online')
                status_text = _clean_text(online_event.new_value, 'unknown')
                world_text = (
                    await self._format_world_display(current.location)
                    if current and current.location
                    else '未知位置'
                )
                joinability = infer_joinability(
                    current.location if current else None,
                    status=current.status if current else online_event.new_value,
                )

                detail_lines = [
                    f"状态：{status_text}",
                    f"位置：{world_text}（{joinability}）",
                ]

                status_change_event = next((item for item in items if item.event_type == 'status_changed'), None)
                if status_change_event:
                    detail_lines.append(
                        f"状态变化：{_clean_text(status_change_event.old_value, 'unknown')} → {_clean_text(status_change_event.new_value, 'unknown')}"
                    )

                location_event = next((item for item in items if item.event_type == 'location_changed'), None)
                if location_event:
                    old_name = await self._get_world_name(location_event.old_value)
                    new_name = await self._get_world_name(location_event.new_value)
                    old_joinability = infer_joinability(location_event.old_value)
                    new_joinability = infer_joinability(
                        location_event.new_value,
                        status=current.status if current else online_event.new_value,
                    )
                    if not (str(location_event.old_value or '').strip().lower() == 'offline'):
                        detail_lines.append(
                            f"切换地图：{old_name}（{old_joinability}） → {new_name}（{new_joinability}）"
                        )

                sign_event = next((item for item in items if item.event_type == 'status_message_changed'), None)
                if sign_event:
                    detail_lines.append(
                        f"签名：{_clean_text(sign_event.old_value)} → {_clean_text(sign_event.new_value)}"
                    )

                messages.append(_build_multiline_message(f"🟢 {shown_name} 上线了", detail_lines))
                continue

            detail_lines = []
            status_change_event = next((item for item in items if item.event_type == 'status_changed'), None)
            if status_change_event:
                detail_lines.append(
                    f"状态变化：{_clean_text(status_change_event.old_value, 'unknown')} → {_clean_text(status_change_event.new_value, 'unknown')}"
                )

            location_event = next((item for item in items if item.event_type == 'location_changed'), None)
            if location_event:
                old_name = await self._get_world_name(location_event.old_value)
                new_name = await self._get_world_name(location_event.new_value)
                current_status = current.status if current else None
                old_joinability = infer_joinability(location_event.old_value)
                new_joinability = infer_joinability(location_event.new_value, status=current_status)
                detail_lines.append(
                    f"切换地图：{old_name}（{old_joinability}） → {new_name}（{new_joinability}）"
                )

            sign_event = next((item for item in items if item.event_type == 'status_message_changed'), None)
            if sign_event:
                detail_lines.append(
                    f"签名：{_clean_text(sign_event.old_value)} → {_clean_text(sign_event.new_value)}"
                )

            if detail_lines:
                messages.append(_build_multiline_message(f"🔄 {shown_name}", detail_lines))
            else:
                for item in items:
                    messages.append(self.monitor.notifier.build_message(item))
        return messages

    async def _push_chain_to_notify_groups(self, components: list) -> int:
        chain = MessageChain(components)
        return await self._send_chain_to_groups(chain)

    def _is_text_mostly_chinese(self, text: str) -> bool:
        if not text:
            return True
        cjk_chars = re.findall(r"[一-鿿]", text)
        ascii_alpha_chars = re.findall(r"[A-Za-z]", text)
        if len(cjk_chars) >= 8:
            return True
        if len(cjk_chars) == 0 and len(ascii_alpha_chars) >= 6:
            return False
        return len(cjk_chars) / max(1, len(text)) >= 0.25

    def _get_translation_lock(self, world_id: str) -> asyncio.Lock:
        key = (world_id or '').strip() or '__unknown_world__'
        lock = self._translation_lock_map.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._translation_lock_map[key] = lock
        return lock

    async def _translate_non_zh_description(self, world_id: str, text: str) -> tuple[str, bool]:
        source = (text or '').strip()
        if not source:
            return "暂无简介", False
        if self._is_text_mostly_chinese(source):
            return source, False

        cached = self.settings_repo.get_world_desc_translation(world_id, source)
        if cached:
            return cached, True

        lock = self._get_translation_lock(world_id)
        async with lock:
            cached = self.settings_repo.get_world_desc_translation(world_id, source)
            if cached:
                return cached, True
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo="vrc_friend_radar:daily_report:translator"
                )
                if not provider_id:
                    using_provider = self.context.get_using_provider()
                    provider_id = using_provider.meta().id if using_provider else ""
                if not provider_id:
                    raise RuntimeError("未找到可用的聊天模型提供商")

                prompt = """请将以下 VRChat 世界简介翻译成简体中文。
要求：
1) 仅输出中文翻译结果，不要解释；
2) 保留专有名词（如世界名、人名）可使用原文；
3) 如果原文过短或无法翻译，尽量给出自然中文表达。"""

                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    contexts=[UserMessageSegment(content=[TextPart(text=f"原文：\n{source}")])],
                    system_prompt="你是一个专业翻译助手。",
                    prompt=prompt,
                )
                translated = (llm_resp.completion_text or '').strip()
                if translated:
                    self.settings_repo.set_world_desc_translation(world_id, source, translated)
                    return translated, True
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 世界简介翻译失败(world={world_id}): {exc}")

            return f"{source}\n（原文）", False

    async def _get_world_info_with_cache(self, world_id: str) -> dict:
        if not world_id:
            return {}
        cached = self.world_cache.get(world_id)
        if cached:
            return cached
        if not self.monitor.client.is_logged_in():
            return {}
        info = await self.monitor.client.get_world_info(world_id)
        if info:
            self.world_cache.set(world_id, info)
            return info
        return {}

    def _format_joinability_overview(self, stats: Counter) -> str:
        if not stats:
            return "未知"
        parts = []
        for key in ("可加入", "不可进入", "未知"):
            count = int(stats.get(key, 0))
            if count > 0:
                parts.append(f"{key}{count}")
        return (" / ".join(parts) if parts else "未知")

    def _get_today_online_friend_ids(self, events: list | None = None) -> list[str]:
        """获取“今天上线过”的好友ID集合（低请求、本地口径）。

        口径：
        1) 今日事件中出现过 friend_online 的好友；
        2) 或当前本地快照中状态非 offline 的好友（已同步到本地的好友）。
        """
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        event_list = events if events is not None else self.db.list_events_between(start, end, friend_ids=None, limit=50000)

        today_ids = {
            item.friend_user_id
            for item in event_list
            if item.event_type == 'friend_online' and (item.friend_user_id or '').strip()
        }

        snapshot_map = self.db.get_friend_snapshot_map()
        for fid, snap in snapshot_map.items():
            if (snap.status or '').strip().lower() == 'offline':
                continue
            updated_at = (snap.updated_at or '').strip()
            # 口径为“今天上线过”：仅纳入今日有更新的在线快照，避免跨天陈旧在线状态污染统计
            if not updated_at or updated_at < start or updated_at > end:
                continue
            today_ids.add(fid)

        return sorted(today_ids)

    async def _collect_hot_world_stats_today(self, top_n: int | None = None, friend_ids: list[str] | None = None) -> list[dict]:
        stat_friend_ids = [fid for fid in (friend_ids or self._get_today_online_friend_ids()) if fid]
        if not stat_friend_ids:
            return []
        top_n = max(1, int(top_n or self.cfg.daily_report_top_n))
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        all_events = self.db.list_events_between(start, end, friend_ids=None, limit=20000)
        events: list = []
        stat_set = set(stat_friend_ids)
        for item in all_events:
            if item.event_type == 'co_room':
                member_ids = [fid for fid in (item.new_value or '').split('|') if fid]
                if any(fid in stat_set for fid in member_ids):
                    events.append(item)
                continue
            if item.friend_user_id in stat_set:
                events.append(item)

        snapshots = self.db.list_friend_snapshots_by_ids(stat_friend_ids)

        stats_map: dict[str, dict] = {}

        def touch(world_id: str, friend_ids: list[str] | None, location: str | None, count_inc: int = 1):
            if not world_id:
                return
            item = stats_map.setdefault(world_id, {
                'world_id': world_id,
                'count': 0,
                'friend_ids': set(),
                'joinability': Counter(),
                'sample_location': '',
            })
            item['count'] += max(1, count_inc)
            for fid in (friend_ids or []):
                if fid:
                    item['friend_ids'].add(fid)
            joinability = infer_joinability(location)
            item['joinability'][joinability] += 1
            if location and not item['sample_location']:
                item['sample_location'] = location

        for event in events:
            if event.event_type == 'location_changed':
                location = event.new_value
                world_id = extract_world_id(location)
                touch(world_id, [event.friend_user_id], location, count_inc=1)
            elif event.event_type == 'co_room':
                location = event.friend_user_id
                world_id = extract_world_id(location)
                member_ids = [fid for fid in (event.new_value or '').split('|') if fid and fid in stat_set]
                touch(world_id, member_ids, location, count_inc=max(1, len(member_ids)))

        for snapshot in snapshots:
            if (snapshot.status or '').strip().lower() == 'offline':
                continue
            world_id = extract_world_id(snapshot.location)
            touch(world_id, [snapshot.friend_user_id], snapshot.location, count_inc=1)

        items = list(stats_map.values())
        items.sort(key=lambda x: (-x['count'], -len(x['friend_ids']), x['world_id']))
        result = []
        for item in items[:top_n]:
            info = await self._get_world_info_with_cache(item['world_id'])
            name = info.get('name') or item['world_id']
            result.append({
                'world_id': item['world_id'],
                'world_name': name,
                'count': int(item['count']),
                'friend_count': len(item['friend_ids']),
                'joinability': item['joinability'],
                'sample_location': item['sample_location'],
                'world_info': info,
            })
        return result

    async def _build_daily_report_components(self) -> list:
        now = datetime.now()
        date_text = now.strftime('%Y-%m-%d')
        top_n = self.cfg.daily_report_top_n
        report_hint = "新增命令 @机器人 vrc灵魂画像 命令可以查看人物画像"

        lines = [f"📘 VRChat 监控日报（{date_text}）"]

        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        all_events = self.db.list_events_between(start, end, friend_ids=None, limit=30000)
        stat_friend_ids = self._get_today_online_friend_ids(events=all_events)
        stat_set = set(stat_friend_ids)

        if not stat_friend_ids:
            lines.append("今日暂无上线好友（基于本地事件与快照）。")
            lines.append("")
            lines.append(report_hint)
            return [Plain("\n".join(lines))]

        events: list = []
        for item in all_events:
            if item.event_type == 'co_room':
                member_ids = [fid for fid in (item.new_value or '').split('|') if fid]
                if any(fid in stat_set for fid in member_ids):
                    events.append(item)
                continue
            if item.friend_user_id in stat_set:
                events.append(item)

        type_counter = Counter(e.event_type for e in events)
        lines.append(
            "今日事件概览："
            f"上线 {type_counter.get('friend_online', 0)} | "
            f"下线 {type_counter.get('friend_offline', 0)} | "
            f"状态变更 {type_counter.get('status_changed', 0)} | "
            f"切换地图 {type_counter.get('location_changed', 0)} | "
            f"同房提醒 {type_counter.get('co_room', 0)}"
        )

        snapshot_map = self.db.get_friend_snapshot_map()
        active_counter = Counter(
            item.friend_user_id
            for item in events
            if item.event_type != 'co_room' and item.friend_user_id in stat_set
        )
        if active_counter:
            lines.append(f"今日活跃好友 Top {top_n}：")
            for idx, (friend_id, cnt) in enumerate(active_counter.most_common(top_n), start=1):
                snapshot = snapshot_map.get(friend_id)
                display = self._sanitize_display_name_for_output(snapshot.display_name if snapshot else '')
                shown_name = display or '未知好友'
                lines.append(f"{idx}. {shown_name}")
        else:
            lines.append("今日活跃好友 Top：暂无可用事件数据。")

        hot_worlds = await self._collect_hot_world_stats_today(top_n=top_n, friend_ids=stat_friend_ids)
        if hot_worlds:
            lines.append(f"今日热门世界 Top {top_n}：")
            for idx, item in enumerate(hot_worlds, start=1):
                overview = self._format_joinability_overview(item['joinability'])
                lines.append(
                    f"{idx}. {item['world_name']} | 热度 {item['count']} | 涉及好友 {item['friend_count']} | {overview}"
                )
        else:
            lines.append("今日热门世界 Top：暂无可用世界数据。")

        lines.append("")
        lines.append(report_hint)

        components = [Plain("\n".join(lines))]

        if hot_worlds:
            recommend = hot_worlds[0]
            info = recommend.get('world_info') or {}
            description = (info.get('description') or '').strip()
            description_zh, translated = await self._translate_non_zh_description(recommend['world_id'], description)
            if len(description_zh) > 220:
                description_zh = description_zh[:220] + '...'
            rec_lines = [
                f"🎯 今日推荐世界：{recommend['world_name']}",
                f"世界ID：{recommend['world_id']}",
                f"简介：{description_zh or '暂无简介'}",
            ]
            if translated:
                rec_lines.append("（注：该简介由世界原文自动翻译，基于 AstrBot AI 生成，可能存在少量语义偏差）")
            components.append(Plain("\n".join(rec_lines)))
            img_url = info.get('thumbnail_image_url') or info.get('image_url') or ''
            if img_url:
                local_img = await self._download_image_to_temp(img_url)
                if local_img:
                    try:
                        components.append(Image.fromFileSystem(local_img))
                    except Exception as exc:
                        logger.error(f"[vrc_friend_radar] 世界推荐图片发送失败: {exc}")
        return components

    def _get_daily_task_last_sent_date(self, task_name: str) -> str:
        return self._daily_task_last_sent_date.get(task_name, '')

    def _set_daily_task_last_sent_date(self, task_name: str, date_text: str) -> None:
        date_text = (date_text or '').strip()
        self._daily_task_last_sent_date[task_name] = date_text
        if task_name == 'daily_report':
            self.settings_repo.set_daily_report_last_sent_date(date_text)

    def _daily_task_should_run(self, task_name: str, now: datetime) -> bool:
        today = now.strftime('%Y-%m-%d')
        if self._get_daily_task_last_sent_date(task_name) == today:
            return False
        task_time = self.cfg.get_daily_task_time(task_name)
        if now.strftime('%H:%M') < task_time:
            return False
        if not self.monitor.get_effective_notify_groups():
            return False
        return True

    async def _send_daily_report_to_notify_groups(self, mark_sent: bool = True) -> int:
        components = await self._build_daily_report_components()
        success = await self._push_chain_to_notify_groups(components)
        if success > 0 and mark_sent:
            today = datetime.now().strftime('%Y-%m-%d')
            self._set_daily_task_last_sent_date('daily_report', today)
        logger.info("[vrc_friend_radar] 日报推送完成: success_groups=%s mark_sent=%s", success, mark_sent)
        return success

    async def _handle_loop_tick(self, now: datetime) -> None:
        if self.cfg.enable_daily_report and self._daily_task_should_run('daily_report', now):
            sent = await self._send_daily_report_to_notify_groups(mark_sent=True)
            if sent > 0:
                logger.info(f"[vrc_friend_radar] 每日任务(daily_report)已发送，日期={now.strftime('%Y-%m-%d')}，群数量={sent}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc状态")
    async def status(self, event: AiocqhttpMessageEvent):
        yield event.plain_result(self.monitor.get_runtime_summary())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc测试")
    async def test_notify(self, event: AiocqhttpMessageEvent):
        yield event.plain_result("VRChat好友雷达插件在线，测试消息发送正常。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc推送测试")
    async def push_test(self, event: AiocqhttpMessageEvent):
        await self._push_messages_to_notify_groups(["🧪 这是一条 VRChat 好友雷达自动推送测试消息。"])
        yield event.plain_result("已尝试向通知群发送测试消息。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc解绑登录")
    async def clear_login(self, event: AiocqhttpMessageEvent):
        self.monitor.clear_persisted_session()
        yield event.plain_result("已清除持久化登录态。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc绑定通知群")
    async def bind_notify_group(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在群聊中使用。")
            return
        self.settings_repo.add_notify_group(group_id)
        groups, _ = self._sync_runtime_config_lists_from_repo()
        yield event.plain_result(f"已绑定通知群 {group_id}，当前通知群数量：{len(groups)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc解绑通知群")
    async def unbind_notify_group(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在群聊中使用。")
            return
        self.settings_repo.remove_notify_group(group_id)
        groups, _ = self._sync_runtime_config_lists_from_repo()
        yield event.plain_result(f"已解绑通知群 {group_id}，当前通知群数量：{len(groups)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc通知群")
    async def show_notify_groups(self, event: AiocqhttpMessageEvent):
        groups = self.monitor.get_effective_notify_groups()
        if not groups:
            yield event.plain_result("当前没有配置通知群。")
            return
        yield event.plain_result("通知群列表：\n" + "\n".join(groups))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc添加监控")
    async def add_watch_friend(self, event: AiocqhttpMessageEvent):
        friend_id = event.message_str.replace("vrc添加监控", "", 1).strip()
        if not friend_id:
            yield event.plain_result("用法：/vrc添加监控 usr_xxx")
            return
        self.settings_repo.add_watch_friend(friend_id)
        _, items = self._sync_runtime_config_lists_from_repo()
        yield event.plain_result(f"已添加监控好友 {friend_id}，当前监控数量：{len(items)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc删除监控")
    async def remove_watch_friend(self, event: AiocqhttpMessageEvent):
        friend_id = event.message_str.replace("vrc删除监控", "", 1).strip()
        if not friend_id:
            yield event.plain_result("用法：/vrc删除监控 usr_xxx")
            return
        self.settings_repo.remove_watch_friend(friend_id)
        _, items = self._sync_runtime_config_lists_from_repo()
        yield event.plain_result(f"已删除监控好友 {friend_id}，当前监控数量：{len(items)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc监控列表")
    async def show_watch_list(self, event: AiocqhttpMessageEvent):
        items = self.monitor.get_effective_watch_friends()
        if not items:
            yield event.plain_result("当前监控好友列表为空。监控提醒/监控事件/同房提醒将不会产生；好友列表与搜索仍可查看全好友缓存。")
            return

        snapshot_map = self.db.get_friend_snapshot_map()
        lines: list[str] = []
        for idx, friend_id in enumerate(items, start=1):
            snapshot = snapshot_map.get(friend_id)
            display_name = self._sanitize_display_name_for_output(snapshot.display_name if snapshot else '')
            shown_name = display_name or friend_id
            lines.append(f"{idx}. {shown_name}")

        yield event.plain_result("监控好友列表：\n" + "\n".join(lines))

    @filter.command("vrc搜索地图")
    async def search_worlds(self, event: AiocqhttpMessageEvent):
        keyword = event.message_str.replace("vrc搜索地图", "", 1).strip()
        if not keyword:
            yield event.plain_result("用法：/vrc搜索地图 关键词")
            return
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("当前未登录，无法搜索地图。")
            return
        results = await self.monitor.client.search_worlds(keyword, limit=5, offset=0)
        if not results:
            yield event.plain_result("没有搜索到匹配地图。")
            return
        header = f"搜索到 {len(results)} 个地图结果："
        first_line = ""
        rest_lines = []
        first_img = ""
        for idx, item in enumerate(results, start=1):
            line = f"{idx}. {item.get('name', '未知地图')} | 作者: {item.get('author_name', '未知')} | ID: {item.get('id', '')}"
            if idx == 1:
                first_line = line
                first_img = item.get('thumbnail_image_url') or item.get('image_url') or ""
            else:
                rest_lines.append(line)
        components = [Plain(header + "\n" + first_line)]
        if first_img:
            local_img = await self._download_image_to_temp(first_img)
            if local_img:
                try:
                    components.append(Image.fromFileSystem(local_img))
                except Exception as exc:
                    logger.error(f"[vrc_friend_radar] 构造本地图像消息失败: {exc}")
        if rest_lines:
            components.append(Plain("\n" + "\n".join(rest_lines)))
        yield event.chain_result(components)

    @filter.command("vrc搜索好友")
    async def search_friends(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc搜索好友", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc搜索好友 关键词 [页码]")
            return
        parts = raw.split()
        page = 1
        if parts and parts[-1].isdigit():
            page = max(1, int(parts[-1]))
            keyword = " ".join(parts[:-1]).strip()
        else:
            keyword = raw
        if not keyword:
            yield event.plain_result("用法：/vrc搜索好友 关键词 [页码]")
            return
        page_size = 10
        total_cached = self.search_repo.count_cached_friends()
        offset = (page - 1) * page_size
        total, items = self.search_repo.search_friends(keyword, limit=page_size, offset=offset)
        if not items:
            yield event.plain_result(f"当前缓存好友总数为 {total_cached}，但没有搜索到匹配好友。请先执行 /vrc同步好友，或换个关键词试试。")
            return
        session_key = self._build_session_key(event)
        self._save_search_session(session_key, items)
        total_pages = max(1, math.ceil(total / page_size))
        lines = [f"在 {total_cached} 个缓存好友中搜索到 {total} 个结果，当前第 {page}/{total_pages} 页："]
        for idx, item in enumerate(items, start=1):
            shown_name = self._sanitize_display_name_for_output(item.display_name)
            lines.append(f"{idx}. {shown_name} | ID: {item.friend_user_id} | 状态: {item.status or 'unknown'} | 位置: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
        lines.append("可使用：/vrc添加监控序号 1")
        if total_pages > page:
            lines.append(f"下一页可用：/vrc搜索好友 {keyword} {page + 1}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc添加监控序号")
    async def add_watch_by_index(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc添加监控序号", "", 1).strip()
        if not raw.isdigit():
            yield event.plain_result("用法：/vrc添加监控序号 1")
            return
        session_key = self._build_session_key(event)
        session = self._get_search_session(session_key)
        if not session:
            yield event.plain_result("当前没有可用的搜索结果，请先执行：/vrc搜索好友 关键词")
            return
        index = int(raw)
        if index < 1 or index > len(session.items):
            yield event.plain_result(f"序号超出范围，请输入 1 到 {len(session.items)} 之间的数字。")
            return
        target = session.items[index - 1]
        self.settings_repo.add_watch_friend(target.friend_user_id)
        _, items = self._sync_runtime_config_lists_from_repo()
        shown_name = self._sanitize_display_name_for_output(target.display_name)
        yield event.plain_result(f"已添加监控好友：{shown_name} | {target.friend_user_id}，当前监控数量：{len(items)}")

    def _parse_login_credentials(self, message_text: str) -> tuple[str, str]:
        # 兼容旧格式：/vrc登录 用户名 密码
        # 新逻辑：第一个参数作为用户名，其后全文原样作为密码（尽量保留空白与特殊符号）
        raw = str(message_text or '').replace("vrc登录", "", 1)
        payload = raw.lstrip()
        if not payload:
            return '', ''

        # 仅按“第一个空白字符”切分一次：
        # - 用户名：首段非空白
        # - 密码：其后全文原样（可包含空格、#、@、:、CQ转义后的字符等）
        split_idx = -1
        for idx, ch in enumerate(payload):
            if ch.isspace():
                split_idx = idx
                break
        if split_idx <= 0:
            return '', ''

        username = payload[:split_idx]
        password = payload[split_idx + 1:]
        return username, password

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc登录")
    async def interactive_login(self, event: AiocqhttpMessageEvent):
        if self._get_group_id(event):
            yield event.plain_result("为了账号安全，请私聊 Bot 发送登录账号和密码，不要在群里发送。")
            return
        self._remember_private_admin_sender(event)
        username, password = self._parse_login_credentials(event.message_str)
        if not username or password == '':
            yield event.plain_result("用法：/vrc登录 用户名 密码")
            return
        logger.info(
            "[vrc_friend_radar] 登录命令解析: username=%s(len=%s, email_like=%s), password_len=%s, contains_ws=%s, leading_ws=%s, trailing_ws=%s",
            username,
            len(username),
            ('@' in username),
            len(password),
            any(ch.isspace() for ch in password),
            bool(password and password[0].isspace()),
            bool(password and password[-1].isspace()),
        )
        session_key = self._build_session_key(event)
        timeout_seconds = self.cfg.login_session_timeout_seconds
        yield event.plain_result("已收到登录请求，正在连接 VRChat，请稍候…")
        login_task = asyncio.create_task(self.monitor.test_login(username=username, password=password))
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(login_task), timeout=10)
            except asyncio.TimeoutError:
                yield event.plain_result("登录请求处理中，VRChat 可能响应较慢，请继续稍候…")
                try:
                    result = await asyncio.wait_for(asyncio.shield(login_task), timeout=max(5, timeout_seconds))
                except asyncio.TimeoutError:
                    login_task.cancel()
                    logger.error(f"[vrc_friend_radar] 登录任务超时(>10s + {max(5, timeout_seconds)}s)，后台线程可能阻塞")
                    yield event.plain_result("登录长时间未完成，已停止本次等待。请稍后重试；若持续复现，请查看日志中的登录阶段(stage)定位卡点。")
                    return
            yield event.plain_result(f"VRChat 登录成功\n用户ID: {result.user_id}\n显示名: {result.display_name}")
            for message in await self._post_login_auto_sync_and_reply(event):
                yield event.plain_result(message)
        except asyncio.TimeoutError:
            login_task.cancel()
            logger.error("[vrc_friend_radar] 登录流程超时（等待初始10秒提示阶段）")
            yield event.plain_result("登录请求超时，请稍后重试。若持续超时，请检查网络或 VRChat 服务状态。")
        except VRChatTwoFactorRequiredError as exc:
            self.monitor.create_pending_login(session_key=session_key, username=username, password=password, method=exc.method)
            if exc.method == "totp_or_recovery":
                yield event.plain_result(f"检测到二步验证，请在{timeout_seconds}秒内发送动态验证码或恢复码：/vrc验证码 123456")
                return
            if exc.method == "email":
                yield event.plain_result(f"检测到邮箱验证码验证，请在{timeout_seconds}秒内发送：/vrc验证码 123456")
                return
            yield event.plain_result(f"检测到额外验证方式 {exc.method}，请在{timeout_seconds}秒内发送：/vrc验证码 123456")
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 登录失败: {exc}")
            if isinstance(exc, VRChatNetworkError):
                yield event.plain_result(f"VRChat 登录失败：网络异常或超时。\n详情：{exc}")
            else:
                yield event.plain_result(f"VRChat 登录失败：{exc}")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 登录流程发生未预期异常: {exc}")
            yield event.plain_result("登录流程异常，请稍后重试或查看日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc验证码")
    async def submit_code(self, event: AiocqhttpMessageEvent):
        if self._get_group_id(event):
            yield event.plain_result("为了账号安全，请私聊 Bot 发送验证码，不要在群里发送。")
            return
        self._remember_private_admin_sender(event)
        code = event.message_str.replace("vrc验证码", "", 1).strip()
        if not code:
            yield event.plain_result("用法：/vrc验证码 123456")
            return
        session_key = self._build_session_key(event)
        pending_key = session_key
        pending = self.monitor.get_pending_login(pending_key)
        if not pending:
            # 兜底：运行中自动恢复触发2FA时，允许管理员在任意私聊上下文提交验证码
            pending_key = '__auto_recover__'
            pending = self.monitor.get_pending_login(pending_key)
        if not pending:
            yield event.plain_result("当前没有等待验证的登录会话，请先发送：/vrc登录 用户名 密码")
            return
        yield event.plain_result("已收到验证码，正在提交验证，请稍候…")
        login_task = asyncio.create_task(
            self.monitor.test_login(username=pending.username, password=pending.password, two_factor_code=code)
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(login_task), timeout=10)
            except asyncio.TimeoutError:
                yield event.plain_result("验证码验证处理中，VRChat 可能响应较慢，请继续稍候…")
                try:
                    result = await asyncio.wait_for(asyncio.shield(login_task), timeout=max(5, self.cfg.login_session_timeout_seconds))
                except asyncio.TimeoutError:
                    login_task.cancel()
                    logger.error(f"[vrc_friend_radar] 验证码提交任务超时(>10s + {max(5, self.cfg.login_session_timeout_seconds)}s)，后台线程可能阻塞")
                    yield event.plain_result("验证码提交后长时间未完成，已停止本次等待。请重试 /vrc验证码 123456，必要时重新 /vrc登录。")
                    return
            self.monitor.pop_pending_login(pending_key)
            yield event.plain_result(f"VRChat 登录成功\n用户ID: {result.user_id}\n显示名: {result.display_name}")
            for message in await self._post_login_auto_sync_and_reply(event):
                yield event.plain_result(message)
        except asyncio.TimeoutError:
            login_task.cancel()
            logger.error("[vrc_friend_radar] 验证码流程超时（等待初始10秒提示阶段）")
            yield event.plain_result("验证码验证超时，请稍后重试 /vrc验证码。若仍失败，可重新执行 /vrc登录。")
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 验证码登录失败: {exc}")
            if isinstance(exc, VRChatNetworkError):
                yield event.plain_result(f"验证码登录失败：网络异常或超时。\n详情：{exc}\n可直接重试 /vrc验证码 123456。")
            else:
                yield event.plain_result(f"验证码登录失败：{exc}，你可以直接重新发送 /vrc验证码 123456 重试。")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 验证码流程发生未预期异常: {exc}")
            yield event.plain_result("验证码处理异常，请稍后重试或重新执行 /vrc登录。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc同步好友")
    async def sync_friends(self, event: AiocqhttpMessageEvent):
        try:
            snapshots = await self.monitor.sync_friends()
            debug = self.monitor.get_sync_debug()
        except VRChatClientError as exc:
            yield event.plain_result(f"同步好友失败：{exc}")
            return
        preview = snapshots[:10]
        total_cached = self.search_repo.count_cached_friends()
        if not preview:
            yield event.plain_result(
                "好友同步完成，但没有获取到任何好友数据。\n"
                f"在线批次返回: {debug.get('online_batch_total', 0)}\n"
                f"离线批次返回: {debug.get('offline_batch_total', 0)}\n"
                f"合并后数量: {debug.get('merged_total', 0)}\n"
                f"白名单数量: {debug.get('filter_count', 0)}"
            )
            return
        lines = [f"好友同步完成，本次写入 {len(snapshots)} 人，当前缓存总数 {total_cached} 人。", f"在线批次返回: {debug.get('online_batch_total', 0)} | 离线批次返回: {debug.get('offline_batch_total', 0)} | 合并后数量: {debug.get('merged_total', 0)}"]
        for idx, item in enumerate(preview, start=1):
            shown_name = self._sanitize_display_name_for_output(item.display_name)
            lines.append(f"{idx}. {shown_name} | 状态: {item.status or 'unknown'} | 地图: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
        if len(snapshots) > len(preview):
            lines.append("更多好友请使用 /vrc好友列表 1 查看。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc好友列表")
    async def friend_list(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc好友列表", "", 1).strip()
        page = int(raw) if raw.isdigit() else 1
        page = max(1, page)
        page_size = 20
        total_cached = self.monitor.count_cached_friends()
        if total_cached <= 0:
            yield event.plain_result("当前没有缓存好友数据，请先执行：/vrc同步好友")
            return
        snapshots = self.monitor.list_cached_friends(limit=page_size, offset=(page - 1) * page_size)
        total_pages = max(1, math.ceil(total_cached / page_size))
        lines = [f"当前缓存好友总数 {total_cached} 人，当前第 {page}/{total_pages} 页："]
        for idx, item in enumerate(snapshots, start=1):
            shown_name = self._sanitize_display_name_for_output(item.display_name)
            lines.append(f"{idx}. {shown_name} | 状态: {item.status or 'unknown'} | 地图: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
        if page < total_pages:
            lines.append(f"下一页可用：/vrc好友列表 {page + 1}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc在线好友")
    async def online_friend_list(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc在线好友", "", 1).strip()
        page = int(raw) if raw.isdigit() else 1
        try:
            yield event.plain_result(await self._build_online_friend_list_message(page=page))
        except VRChatClientError as exc:
            yield event.plain_result(f"获取在线好友失败：{exc}")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 查询在线好友异常: {exc}")
            yield event.plain_result("获取在线好友时发生异常，请稍后重试。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc检测变化")
    async def detect_changes(self, event: AiocqhttpMessageEvent):
        try:
            events = await self.monitor.detect_changes()
        except VRChatClientError as exc:
            yield event.plain_result(f"检测变化失败：{exc}")
            return
        if not events:
            yield event.plain_result("本次检测没有发现好友状态变化。")
            return
        messages = await self._format_events_for_push(events)
        yield event.plain_result(self.monitor.notifier.build_batch_message(messages))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc同房情况")
    async def coroom_status(self, event: AiocqhttpMessageEvent):
        groups = self.monitor.list_coroom_groups()
        if not groups:
            yield event.plain_result(f"当前没有监控好友同处同一实例（至少{self.cfg.coroom_notify_min_members}人）的情况。")
            return
        lines = [f"当前同房实例 {len(groups)} 个："]
        for idx, group in enumerate(groups, start=1):
            members = group.get('members', [])
            names = [self._sanitize_display_name_for_output(item.display_name) for item in members]
            location_key = group.get('location_key', '')
            world_text = await self._format_world_display(location_key)
            joinability = infer_joinability(location_key)
            lines.append(f"{idx}. {world_text} | 人数: {len(members)} | {joinability} | 成员: {'、'.join(names)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("vrc热门世界")
    async def hot_worlds(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc热门世界", "", 1).strip()
        top_n = int(raw) if raw.isdigit() else self.cfg.daily_report_top_n
        top_n = max(1, min(20, top_n))
        try:
            items = await self._collect_hot_world_stats_today(top_n=top_n)
        except VRChatClientError as exc:
            yield event.plain_result(f"获取热门世界失败：{exc}")
            return
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 统计热门世界异常: {exc}")
            yield event.plain_result("获取热门世界时发生异常，请稍后重试。")
            return
        if not items:
            yield event.plain_result("今日暂无热门世界统计数据（今日暂无上线好友）。")
            return
        lines = [f"今日热门世界 Top {top_n}（当天上线好友）:"]
        for idx, item in enumerate(items, start=1):
            overview = self._format_joinability_overview(item['joinability'])
            lines.append(f"{idx}. {item['world_name']} | 热度 {item['count']} | 涉及好友 {item['friend_count']} | {overview}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc生成日报")
    async def generate_daily_report(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc生成日报", "", 1).strip()
        if raw == "推送":
            sent = await self._send_daily_report_to_notify_groups(mark_sent=False)
            if sent <= 0:
                yield event.plain_result("日报推送失败：当前无可用通知群或发送异常。")
            else:
                yield event.plain_result(f"已向 {sent} 个通知群推送日报（手动推送不计入自动去重日期）。")
            return
        components = await self._build_daily_report_components()
        yield event.chain_result(components)

    async def _build_public_soul_profile_image(self, event: AiocqhttpMessageEvent, raw_target: str) -> str:
        friend_id, _ = self._resolve_profile_target(raw_target)
        summary = await self._build_soul_profile_summary(event, friend_id)
        return await self._render_soul_profile_card(summary)

    @filter.command("vrc\u7075\u9b42\u753b\u50cf")
    async def weekly_soul_profile(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc\u7075\u9b42\u753b\u50cf", "", 1).strip()
        if not raw:
            yield event.plain_result("\u7528\u6cd5\uff1a/vrc\u7075\u9b42\u753b\u50cf \u7528\u6237\u540d\u5b57")
            return
        try:
            friend_id, _ = await self._resolve_profile_target_interactive(event, raw, "\u751f\u6210\u7075\u9b42\u753b\u50cf")
            image_url = await self._build_public_soul_profile_image(event, friend_id)
            yield event.image_result(image_url)
        except VRChatClientError as exc:
            yield event.plain_result(f"\u751f\u6210\u7075\u9b42\u753b\u50cf\u5931\u8d25\uff1a{exc}")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] \u751f\u6210\u7075\u9b42\u753b\u50cf\u5f02\u5e38: {exc}")
            yield event.plain_result("\u751f\u6210\u7075\u9b42\u753b\u50cf\u65f6\u53d1\u751f\u5f02\u5e38\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")

    @filter.command("vrc\u4eba\u8bbe")
    async def persona_only(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc\u4eba\u8bbe", "", 1).strip()
        if not raw:
            yield event.plain_result("\u7528\u6cd5\uff1a/vrc\u4eba\u8bbe \u663e\u793a\u540d")
            return
        try:
            friend_id, _ = await self._resolve_profile_target_interactive(event, raw, "\u751f\u6210 AI \u4eba\u8bbe")
            summary = await self._build_soul_profile_summary(event, friend_id)
            yield event.plain_result(summary.ai_persona_text)
        except VRChatClientError as exc:
            yield event.plain_result(f"\u751f\u6210\u4eba\u8bbe\u5931\u8d25\uff1a{exc}")

    @filter.command("\u547d\u8fd0\u6307\u5f15")
    async def fortune_only(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("\u547d\u8fd0\u6307\u5f15", "", 1).strip()
        if not raw:
            yield event.plain_result("\u7528\u6cd5\uff1a/\u547d\u8fd0\u6307\u5f15 \u663e\u793a\u540d")
            return
        try:
            friend_id, _ = await self._resolve_profile_target_interactive(event, raw, "\u751f\u6210\u547d\u8fd0\u6307\u5f15")
            summary = await self._build_soul_profile_summary(event, friend_id)
            yield event.plain_result(summary.ai_fortune_text)
        except VRChatClientError as exc:
            yield event.plain_result(f"\u751f\u6210\u547d\u8fd0\u6307\u5f15\u5931\u8d25\uff1a{exc}")

    @filter.command("vrc\u7f18\u5206")
    async def relationship_score(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc\u7f18\u5206", "", 1).strip()
        if not raw:
            yield event.plain_result("\u7528\u6cd5\uff1a/vrc\u7f18\u5206 \u663e\u793a\u540d")
            return
        try:
            target_id, target_name = await self._resolve_profile_target_interactive(event, raw, "\u5bfb\u627e\u6700\u6709\u7f18\u7684\u4eba")
            now = datetime.now()
            days = max(1, int(self.cfg.soul_profile_days or 7))
            start_dt = now - timedelta(days=days)
            snapshot_map = self.db.get_friend_snapshot_map()
            partner_id, partner_name, overlap_minutes, overlap_worlds = self._estimate_companion_match(
                target_id,
                snapshot_map,
                start_dt,
                now,
            )
            if not partner_id or overlap_minutes <= 0:
                yield event.plain_result(f"{target_name} \u6700\u8fd1 {days} \u5929\u8fd8\u6ca1\u6709\u627e\u5230\u7279\u522b\u7a33\u5b9a\u7684\u540c\u6e38\u5bf9\u8c61\uff0c\u50cf\u662f\u5728\u7b49\u4e00\u6bb5\u66f4\u521a\u597d\u7684\u7f18\u5206\u6162\u6162\u9760\u8fd1\u3002")
                return

            score = min(99, max(36, overlap_worlds * 18 + overlap_minutes // 18))
            hours = overlap_minutes // 60
            minutes = overlap_minutes % 60
            duration_text = f"{hours}\u5c0f\u65f6{minutes}\u5206\u949f" if hours else f"{minutes}\u5206\u949f"
            yield event.plain_result(
                f"\u59fb\u7f18\u7b7e\uff1a\u8fd1 {days} \u5929\u91cc\uff0c{target_name} \u547d\u76d8\u91cc\u6700\u5bb9\u6613\u548c {partner_name} \u76f8\u4e92\u7167\u4eae\u3002\n"
                f"\u7b7e\u6587\u663e\u793a\uff0c\u4f60\u4eec\u5728\u76f8\u8fd1\u5730\u56fe\u91cc\u7d2f\u79ef\u76f8\u4f34\u7ea6 {duration_text}\uff0c\u91cd\u5408\u4e16\u754c {overlap_worlds} \u5904\uff0c\u59fb\u7f18\u503c\u7ea6\u4e3a {score}/99\u3002\n"
                f"\u8fd9\u662f\u4e00\u652f\u201c\u6162\u70ed\u540c\u5fc3\u7b7e\u201d\uff0c\u7f18\u5206\u4e0d\u662f\u4e00\u773c\u60ca\u8273\uff0c\u800c\u662f\u6b21\u6b21\u540c\u8def\u540e\u60c5\u7eea\u6084\u6084\u843d\u5728\u540c\u4e00\u4e2a\u8282\u62cd\u91cc\u3002"
                f"\u82e5\u8fd9\u6bb5\u7f18\u7ebf\u7ee7\u7eed\u5f80\u524d\u8d70\uff0c\u5f88\u5bb9\u6613\u4ece\u201c\u521a\u597d\u540c\u884c\u201d\u6162\u6162\u957f\u6210\u201c\u4e92\u76f8\u60e6\u8bb0\u201d\u7684\u67d4\u8f6f\u6545\u4e8b\u3002"
            )
        except VRChatClientError as exc:
            yield event.plain_result(f"\u8ba1\u7b97\u7f18\u5206\u6307\u6570\u5931\u8d25\uff1a{exc}")

    @filter.command("vrc\u52a0\u597d\u53cb")
    async def public_friend_request(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc\u52a0\u597d\u53cb", "", 1).strip()
        if not raw:
            yield event.plain_result("\u7528\u6cd5\uff1a/vrc\u52a0\u597d\u53cb usr_xxx")
            return
        if not self._is_public_friend_request_allowed():
            yield event.plain_result("\u5f53\u524d\u7ba1\u7406\u5458\u8fd8\u6ca1\u6709\u5f00\u653e\u516c\u5171\u52a0\u597d\u53cb\u529f\u80fd\u3002")
            return
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("\u673a\u5668\u4eba\u5f53\u524d\u8fd8\u6ca1\u6709\u767b\u5f55 VRChat \u8d26\u53f7\uff0c\u6682\u65f6\u65e0\u6cd5\u4ee3\u53d1\u597d\u53cb\u7533\u8bf7\u3002")
            return
        target = raw.split()[0].strip()
        if not re.fullmatch(r"usr_[A-Za-z0-9_-]+", target, flags=re.IGNORECASE):
            yield event.plain_result("\u8bf7\u8f93\u5165\u6b63\u786e\u7684 VRChat \u7528\u6237 ID\uff0c\u4f8b\u5982 usr_xxx\u3002")
            return
        try:
            await self.monitor.client.send_friend_request(target)
            yield event.plain_result(f"\u5df2\u7ecf\u5e2e\u4f60\u5411 {target} \u53d1\u51fa\u597d\u53cb\u7533\u8bf7\u5566\uff0c\u63a5\u4e0b\u6765\u5c31\u6e29\u67d4\u5730\u7b49\u5bf9\u65b9\u5728 VRChat \u91cc\u56de\u5e94\u5427\u3002")
        except VRChatClientError as exc:
            yield event.plain_result(f"\u53d1\u9001\u597d\u53cb\u7533\u8bf7\u5931\u8d25\uff1a{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc\u516c\u5171\u52a0\u597d\u53cb")
    async def toggle_public_friend_request(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc\u516c\u5171\u52a0\u597d\u53cb", "", 1).strip()
        if raw not in {"\u5f00\u542f", "\u5173\u95ed"}:
            current = "\u5f00\u542f" if self._is_public_friend_request_allowed() else "\u5173\u95ed"
            yield event.plain_result(f"\u5f53\u524d\u516c\u5171\u52a0\u597d\u53cb\u72b6\u6001\uff1a{current}\n\u7528\u6cd5\uff1a/vrc\u516c\u5171\u52a0\u597d\u53cb \u5f00\u542f \u6216 /vrc\u516c\u5171\u52a0\u597d\u53cb \u5173\u95ed")
            return
        enabled = raw == "\u5f00\u542f"
        self._set_public_friend_request_allowed(enabled)
        yield event.plain_result(f"\u516c\u5171\u52a0\u597d\u53cb\u529f\u80fd\u5df2{raw}\u3002")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc\u6700\u8fd1\u4e8b\u4ef6")
    async def recent_events(self, event: AiocqhttpMessageEvent):
        events = self.monitor.list_recent_events(limit=20)
        if not events:
            yield event.plain_result("当前没有事件历史。")
            return
        lines = ["最近事件："]
        for idx, item in enumerate(events, start=1):
            lines.append(f"{idx}. {item.event_type} | {item.friend_user_id} | {item.old_value or '空'} -> {item.new_value or '空'}")
        yield event.plain_result("\n".join(lines))
