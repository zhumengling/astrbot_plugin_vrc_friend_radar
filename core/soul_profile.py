"""灵魂画像核心逻辑与渲染 Mixin - 摘要构建、卡片渲染、AI文案生成等，由 VRCFriendRadarPlugin 继承使用。"""
from __future__ import annotations
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PIL import Image as PILImage, ImageDraw, ImageFilter, ImageFont
from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.io import save_temp_img
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .utils import extract_world_id, format_location, infer_joinability
from .vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

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

class SoulProfileMixin:
    """灵魂画像核心逻辑 Mixin，self 即为插件实例。"""

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

    @staticmethod
    def _split_name_and_extras(raw: str) -> tuple[str, str]:
        """把 `名字 [| 附加]` / `usr_xxx 附加` 分成 (名字或ID, 剩余参数字符串)。

        规则：
        - 包含管道符（| 或 ｜）→ 以第一个管道符为分隔
        - 否则若首 token 是 usr_xxx → 首 token 为 ID，剩余为附加
        - 否则：名字可能含空格，整段作为名字，附加为空（命令里若需要附加参数必须使用 `|`）
        """
        text = str(raw or '').strip()
        if not text:
            return '', ''
        normalized = text.replace('｜', '|')
        if '|' in normalized:
            left, right = normalized.split('|', 1)
            return left.strip(), right.strip()
        parts = text.split(None, 1)
        if parts and re.fullmatch(r'usr_[A-Za-z0-9_-]+', parts[0], flags=re.IGNORECASE):
            return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else '')
        return text, ''

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

        top_world_names = '、'.join(item['world_name'] for item in top_world_rows[:2]) or '柔软角落'
        overview_text = (
            f"\u6700\u8fd1{max(1, len(day_marks))}\u5929\u91cc\uff0c{display_name}\u7559\u4e0b\u4e86{len(events)}\u6761\u53ef\u4ee5\u88ab\u770b\u89c1\u7684\u8db3\u8ff9\uff0c"
            f"\u6700\u5e38\u505c\u7559\u5728{top_world_names}\u3002"
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
        active_periods_text = '\u3001'.join(summary.active_periods) or '\u884c\u8e2a\u8f7b\u76c8'
        y_right = draw_card(
            right_x,
            y_right,
            right_width,
            '\u6d3b\u8dc3\u65f6\u6bb5\u4e0e\u65c5\u884c\u6807\u7b7e',
            [f"\u6d3b\u8dc3\u65f6\u6bb5\uff1a{active_periods_text}", f"\u65c5\u884c\u6807\u7b7e\uff1a{tags_line}"],
        )
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

