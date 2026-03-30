import math
import tempfile
import uuid
import asyncio
from pathlib import Path
import time
import re
from collections import Counter
from datetime import datetime, timedelta
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools
from astrbot.core.agent.message import UserMessageSegment, TextPart

from .core.config import PluginConfig
from .core.db import RadarDB
from .core.monitor import MonitorService
from .core.repository import SearchRepository, SettingsRepository
from .core.search_state import SearchSession
from .core.utils import extract_world_id, format_location, infer_joinability
from .core.vrchat_client import VRChatClientError, VRChatTwoFactorRequiredError
from .core.world_cache import WorldCache


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
        self._search_sessions: dict[str, SearchSession] = {}
        self._daily_task_last_sent_date: dict[str, str] = {"daily_report": ""}
        self._translation_lock_map: dict[str, asyncio.Lock] = {}

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
        self.db.initialize()
        self.settings_repo.initialize()
        merged_notify_groups, merged_watch_friends = self._reconcile_dynamic_lists_on_startup()
        self._daily_task_last_sent_date["daily_report"] = self.settings_repo.get_daily_report_last_sent_date()
        await self.monitor.start()
        logger.info(
            "[vrc_friend_radar] 插件初始化完成，已同步列表: notify_groups=%s, watch_friends=%s",
            len(merged_notify_groups),
            len(merged_watch_friends),
        )

    async def terminate(self):
        await self.monitor.stop()
        self._search_sessions.clear()
        logger.info("[vrc_friend_radar] 插件已停止")

    async def _handle_monitor_events(self, events) -> None:
        messages = await self._format_events_for_push(events)
        await self._push_messages_to_notify_groups(messages)

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
        return f"{event.unified_msg_origin}:{sender_id}"

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
        try:
            if getattr(event.message_obj, 'group_id', None):
                return str(event.message_obj.group_id)
        except Exception:
            pass
        return None

    def _is_private_event(self, event: AiocqhttpMessageEvent) -> bool:
        return self._get_group_id(event) is None

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
            lines.append(f"{idx}. {item.display_name} | 状态: {item.status or 'unknown'} | 地图: {world_text} | {infer_joinability(item.location, status=item.status)}")
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
        file_path = temp_dir / f"world_logo_{uuid.uuid4().hex}{suffix}"
        try:
            return await self.monitor.client.download_image_authenticated(url, str(file_path))
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 下载地图Logo失败: {exc}")
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
        messages = []
        snapshot_map = self.db.get_friend_snapshot_map()
        for item in events[: self.cfg.event_batch_size]:
            if item.event_type == 'location_changed':
                old_name = await self._get_world_name(item.old_value)
                new_name = await self._get_world_name(item.new_value)
                current_snapshot = snapshot_map.get(item.friend_user_id)
                current_status = current_snapshot.status if current_snapshot else None
                messages.append(
                    self.monitor.notifier.build_location_change_message(
                        item.display_name,
                        old_name,
                        new_name,
                        item.old_value,
                        item.new_value,
                        status=current_status,
                    )
                )
                continue
            if item.event_type == 'friend_online':
                current = snapshot_map.get(item.friend_user_id)
                joinability = infer_joinability(
                    current.location if current else None,
                    status=current.status if current else item.new_value,
                )
                world_text = (
                    await self._format_world_display(current.location)
                    if current
                    else '未知位置'
                )
                messages.append(
                    f"🟢 {item.display_name} 上线啦 | 状态：{item.new_value or 'unknown'} | 位置：{world_text} | {joinability}"
                )
                continue
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
        for key in ("可加入", "需邀请", "不可加入", "未知"):
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
        events = self.db.list_events_between(start, end, friend_ids=stat_friend_ids, limit=20000)
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

        stat_set = set(stat_friend_ids)
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

        lines = [f"📘 VRChat 监控日报（{date_text}）"]

        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        all_events = self.db.list_events_between(start, end, friend_ids=None, limit=30000)
        stat_friend_ids = self._get_today_online_friend_ids(events=all_events)
        stat_set = set(stat_friend_ids)

        if not stat_friend_ids:
            lines.append("今日暂无上线好友（基于本地事件与快照）。")
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
                display = snapshot_map.get(friend_id).display_name if snapshot_map.get(friend_id) else friend_id
                lines.append(f"{idx}. {display}（{friend_id}）- 事件 {cnt}")
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
        yield event.plain_result("监控好友列表：\n" + "\n".join(items))

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
            lines.append(f"{idx}. {item.display_name} | ID: {item.friend_user_id} | 状态: {item.status or 'unknown'} | 位置: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
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
        yield event.plain_result(f"已添加监控好友：{target.display_name} | {target.friend_user_id}，当前监控数量：{len(items)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc登录")
    async def interactive_login(self, event: AiocqhttpMessageEvent):
        if self._get_group_id(event):
            yield event.plain_result("为了账号安全，请私聊 Bot 发送登录账号和密码，不要在群里发送。")
            return
        raw = event.message_str.replace("vrc登录", "", 1).strip()
        parts = raw.split()
        if len(parts) < 2:
            yield event.plain_result("用法：/vrc登录 用户名 密码")
            return
        username = parts[0].strip()
        password = parts[1].strip()
        session_key = self._build_session_key(event)
        timeout_seconds = self.cfg.login_session_timeout_seconds
        try:
            result = await self.monitor.test_login(username=username, password=password)
            yield event.plain_result(f"VRChat 登录成功\n用户ID: {result.user_id}\n显示名: {result.display_name}")
            for message in await self._post_login_auto_sync_and_reply(event):
                yield event.plain_result(message)
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
            yield event.plain_result(f"VRChat 登录失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc验证码")
    async def submit_code(self, event: AiocqhttpMessageEvent):
        code = event.message_str.replace("vrc验证码", "", 1).strip()
        if not code:
            yield event.plain_result("用法：/vrc验证码 123456")
            return
        session_key = self._build_session_key(event)
        pending = self.monitor.get_pending_login(session_key)
        if not pending:
            yield event.plain_result("当前没有等待验证的登录会话，请先发送：/vrc登录 用户名 密码")
            return
        try:
            result = await self.monitor.test_login(username=pending.username, password=pending.password, two_factor_code=code)
            self.monitor.pop_pending_login(session_key)
            yield event.plain_result(f"VRChat 登录成功\n用户ID: {result.user_id}\n显示名: {result.display_name}")
            for message in await self._post_login_auto_sync_and_reply(event):
                yield event.plain_result(message)
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 验证码登录失败: {exc}")
            yield event.plain_result(f"验证码登录失败：{exc}，你可以直接重新发送 /vrc验证码 123456 重试。")

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
            lines.append(f"{idx}. {item.display_name} | 状态: {item.status or 'unknown'} | 地图: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
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
            lines.append(f"{idx}. {item.display_name} | 状态: {item.status or 'unknown'} | 地图: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
        if page < total_pages:
            lines.append(f"下一页可用：/vrc好友列表 {page + 1}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc在线好友")
    async def online_friend_list(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc在线好友", "", 1).strip()
        page = int(raw) if raw.isdigit() else 1
        yield event.plain_result(await self._build_online_friend_list_message(page=page))

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
            names = [item.display_name for item in members]
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
        items = await self._collect_hot_world_stats_today(top_n=top_n)
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc最近事件")
    async def recent_events(self, event: AiocqhttpMessageEvent):
        events = self.monitor.list_recent_events(limit=20)
        if not events:
            yield event.plain_result("当前没有事件历史。")
            return
        lines = ["最近事件："]
        for idx, item in enumerate(events, start=1):
            lines.append(f"{idx}. {item.event_type} | {item.friend_user_id} | {item.old_value or '空'} -> {item.new_value or '空'}")
        yield event.plain_result("\n".join(lines))
