import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from .config import PluginConfig
from .db import RadarDB
from .diff import diff_snapshot
from .models import FriendSnapshot, RadarEvent
from .notifier import Notifier
from .repository import SettingsRepository
from .session_store import SessionStore
from .utils import get_location_group_key, infer_joinability
from astrbot.api import logger
from .vrchat_client import LoginResult, VRChatClient, VRChatClientError


@dataclass(slots=True)
class PendingLoginSession:
    session_key: str
    username: str
    password: str
    created_at: float
    method: str = "unknown"

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.created_at) > ttl_seconds


class MonitorService:
    def __init__(self, cfg: PluginConfig, db: RadarDB, settings_repo: SettingsRepository):
        self.cfg = cfg
        self.db = db
        self.settings_repo = settings_repo
        self.notifier = Notifier()
        self.client = VRChatClient(cfg.vrchat_user_agent)
        self.session_store = SessionStore(cfg.data_dir)
        self._task: asyncio.Task | None = None
        self._running = False
        self._tick_count = 0
        self._last_login_result: LoginResult | None = None
        self._pending_logins: dict[str, PendingLoginSession] = {}
        self._last_sync_count = 0
        self._last_detected_events: list[RadarEvent] = []
        self._event_callback: Callable[[list[RadarEvent]], Awaitable[None]] | None = None
        self._loop_tick_callback: Callable[[datetime], Awaitable[None]] | None = None
        self._last_coroom_notify_at: dict[str, float] = {}
        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(self.cfg.read_notify_group_ids_from_raw())
        self._last_seen_raw_watch_friends = self._dedupe_clean_ids(self.cfg.read_watch_friend_ids_from_raw())
        self._stop_event = asyncio.Event()

    async def try_restore_session(self) -> bool:
        data = self.session_store.load()
        if not data:
            return False
        try:
            result = await self.client.restore_session(data.get('username', ''), data.get('password', ''), data.get('cookie', ''))
            self._last_login_result = result
            return True
        except VRChatClientError:
            self.session_store.clear()
            return False

    def persist_session(self) -> None:
        data = self.client.export_session()
        if data:
            self.session_store.save(data)

    def clear_persisted_session(self) -> None:
        self.session_store.clear()

    def set_event_callback(self, callback: Callable[[list[RadarEvent]], Awaitable[None]]) -> None:
        self._event_callback = callback

    def set_loop_tick_callback(self, callback: Callable[[datetime], Awaitable[None]]) -> None:
        self._loop_tick_callback = callback

    def get_effective_notify_groups(self) -> list[str]:
        raw_groups = self._dedupe_clean_ids(self.cfg.read_notify_group_ids_from_raw())
        runtime_groups = self._dedupe_clean_ids(self.cfg.notify_group_ids)
        repo_groups = self._dedupe_clean_ids(self.settings_repo.get_notify_groups())

        # 外部配置发生变更（WebUI/配置热更新）：以当前配置视图为准，覆盖到持久化
        if raw_groups != self._last_seen_raw_notify_groups:
            self.settings_repo.set_notify_groups(raw_groups)
            self.cfg.sync_runtime_lists(notify_group_ids=raw_groups, write_back_raw=True)
            result = raw_groups
        else:
            # 命令/数据库改动：以持久化为准同步到运行时配置
            if repo_groups != runtime_groups:
                self.cfg.sync_runtime_lists(notify_group_ids=repo_groups, write_back_raw=True)
                result = repo_groups
            else:
                result = runtime_groups

        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(self.cfg.read_notify_group_ids_from_raw())
        return result

    def get_effective_watch_friends(self) -> list[str]:
        raw_friend_ids = self._dedupe_clean_ids(self.cfg.read_watch_friend_ids_from_raw())
        runtime_friend_ids = self._dedupe_clean_ids(self.cfg.watch_friend_ids)
        repo_friend_ids = self._dedupe_clean_ids(self.settings_repo.get_watch_friends())

        # 外部配置发生变更（WebUI/配置热更新）：以当前配置视图为准，覆盖到持久化
        if raw_friend_ids != self._last_seen_raw_watch_friends:
            self.settings_repo.set_watch_friends(raw_friend_ids)
            self.cfg.sync_runtime_lists(watch_friend_ids=raw_friend_ids, write_back_raw=True)
            result = raw_friend_ids
        else:
            # 命令/数据库改动：以持久化为准同步到运行时配置
            if repo_friend_ids != runtime_friend_ids:
                self.cfg.sync_runtime_lists(watch_friend_ids=repo_friend_ids, write_back_raw=True)
                result = repo_friend_ids
            else:
                result = runtime_friend_ids

        self._last_seen_raw_watch_friends = self._dedupe_clean_ids(self.cfg.read_watch_friend_ids_from_raw())
        self._stop_event = asyncio.Event()

        # 监控列表对外展示时，watch_self=true 需体现到 effective list（不写回配置/数据库）
        if self.cfg.watch_self:
            self_id = (self.client.get_current_user_id() or '').strip()
            if self_id:
                result = self._dedupe_clean_ids([*result, self_id])
        return result

    def get_monitor_watch_friend_ids(self) -> list[str]:
        """监控语义统一入口：监控名单 + 可选本人。"""
        # get_effective_watch_friends 已处理 watch_self 的可见性，这里仅做一次去重。
        return self._dedupe_clean_ids(self.get_effective_watch_friends())

    @staticmethod
    def _should_track_self_location_change(old_location: str | None, new_location: str | None) -> bool:
        # 本人监控时，location 不可见/未知/私密等场景噪声较高，仅跟踪可识别世界实例之间的切换
        return bool(get_location_group_key(old_location)) and bool(get_location_group_key(new_location))

    @staticmethod
    def _dedupe_clean_ids(friend_ids: list[str] | None) -> list[str]:
        items: list[str] = []
        seen: set[str] = set()
        for friend_id in friend_ids or []:
            value = str(friend_id or '').strip()
            if not value or value in seen:
                continue
            seen.add(value)
            items.append(value)
        return items

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        await self.try_restore_session()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._pending_logins.clear()
        self.client.close()

    async def _run_loop(self) -> None:
        while self._running:
            self._tick_count += 1
            self._cleanup_pending_logins()
            if self.cfg.allow_auto_push and self.client.is_logged_in():
                try:
                    detect_timeout = max(10, min(self.cfg.poll_interval_seconds - 5, 120))
                    events = await asyncio.wait_for(self.detect_changes(), timeout=detect_timeout)
                    if events and self._event_callback:
                        try:
                            await self._event_callback(events)
                        except Exception as exc:
                            logger.error(f"[vrc_friend_radar] 事件回调执行失败: {exc}", exc_info=True)
                except asyncio.TimeoutError:
                    logger.error("[vrc_friend_radar] 轮询检测超时，已跳过本轮")
                except Exception as exc:
                    logger.error(f"[vrc_friend_radar] 轮询检测失败: {exc}", exc_info=True)
            if self._loop_tick_callback:
                try:
                    await self._loop_tick_callback(datetime.now())
                except Exception as exc:
                    logger.error(f"[vrc_friend_radar] 轮询Tick回调执行失败: {exc}", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=max(1, self.cfg.poll_interval_seconds))
                break
            except asyncio.TimeoutError:
                continue

    async def test_login(self, username: str, password: str, two_factor_code: str | None = None) -> LoginResult:
        result = await self.client.login(username=username, password=password, two_factor_code=two_factor_code)
        self._last_login_result = result
        self.persist_session()
        return result

    async def sync_friends(self) -> list[FriendSnapshot]:
        # 信息浏览类能力（好友列表/搜索/在线列表）保持全好友范围
        snapshots = await self.client.fetch_friend_snapshots(None)
        self.db.upsert_friend_snapshots(snapshots)
        self._last_sync_count = len(snapshots)
        return snapshots

    def get_sync_debug(self) -> dict[str, int]:
        return self.client.get_last_sync_debug()

    async def _resolve_self_context(self) -> tuple[str, FriendSnapshot | None]:
        """
        解析本轮监控所需的本人信息。
        - watch_self=False: 直接跳过；
        - watch_self=True 且 current_user_id 为空: 主动请求当前用户快照以刷新 user_id；
        返回：(self_user_id, self_snapshot)
        """
        if not self.cfg.watch_self:
            return '', None

        self_id = (self.client.get_current_user_id() or '').strip()
        self_snapshot: FriendSnapshot | None = None
        if self_id:
            return self_id, None

        # 关键兜底：某些恢复登录/重启场景下 current_user_id 可能为空，主动拉取一次本人快照
        self_snapshot = await self.client.fetch_self_snapshot()
        if self_snapshot is not None:
            self_id = (self_snapshot.friend_user_id or '').strip()

        if not self_id:
            logger.warning('[vrc_friend_radar] watch_self=true 但当前无法获取 current_user_id，本轮仅监控普通好友')
        else:
            logger.info('[vrc_friend_radar] watch_self=true 且 current_user_id 缺失，已通过 fetch_self_snapshot() 刷新')
        return self_id, self_snapshot

    async def detect_changes(self) -> list[RadarEvent]:
        base_watch_ids = self._dedupe_clean_ids(self.get_effective_watch_friends())
        resolved_self_id, self_snapshot = await self._resolve_self_context()

        watch_ids = base_watch_ids
        if resolved_self_id:
            watch_ids = self._dedupe_clean_ids([*watch_ids, resolved_self_id])

        if not watch_ids:
            logger.info("[vrc_friend_radar] 本轮变化检测跳过：监控名单为空")
            self._last_sync_count = 0
            self._last_detected_events = []
            return []

        watch_set = set(watch_ids)
        old_map_all = self.db.get_friend_snapshot_map()
        old_map = {friend_id: old_map_all[friend_id] for friend_id in watch_ids if friend_id in old_map_all}
        new_snapshots = await self.client.fetch_friend_snapshots(watch_ids)

        if resolved_self_id and resolved_self_id in watch_set:
            if self_snapshot is None:
                self_snapshot = await self.client.fetch_self_snapshot()
            if self_snapshot is not None and (self_snapshot.friend_user_id or '').strip():
                # 以当前登录账号实时快照为准，覆盖同ID项（理论上好友列表不应包含自己）
                merged = {item.friend_user_id: item for item in new_snapshots}
                merged[self_snapshot.friend_user_id] = self_snapshot
                new_snapshots = list(merged.values())

        new_snapshots = [item for item in new_snapshots if item.friend_user_id in watch_set]

        raw_events: list[RadarEvent] = []
        for item in new_snapshots:
            old_item = old_map.get(item.friend_user_id)
            if old_item is None:
                continue
            raw_events.extend(diff_snapshot(old_item, item))

        filtered_events: list[RadarEvent] = []
        skipped_status_events = 0
        skipped_world_events = 0
        current_self_id = resolved_self_id or (self.client.get_current_user_id() or '').strip()
        for event in raw_events:
            if event.event_type in {"friend_online", "friend_offline", "status_changed", "status_message_changed"}:
                if not self.cfg.enable_status_tracking:
                    skipped_status_events += 1
                    continue
                if (
                    current_self_id
                    and event.friend_user_id == current_self_id
                    and event.event_type in {"friend_online", "friend_offline", "status_changed"}
                ):
                    old_status = str(event.old_value or '').strip().lower()
                    new_status = str(event.new_value or '').strip().lower()
                    if {old_status, new_status}.issubset({'offline', 'web_online'}):
                        skipped_status_events += 1
                        logger.info(
                            "[vrc_friend_radar] self status web-presence transition suppressed: old=%s, new=%s",
                            event.old_value,
                            event.new_value,
                        )
                        continue
            if event.event_type == "location_changed":
                if not self.cfg.enable_world_tracking:
                    skipped_world_events += 1
                    continue
                if current_self_id and event.friend_user_id == current_self_id and not self._should_track_self_location_change(event.old_value, event.new_value):
                    skipped_world_events += 1
                    continue
            filtered_events.append(event)

        events = self._dedupe_events(filtered_events)
        self.db.upsert_friend_snapshots(new_snapshots)
        coroom_events = self._build_coroom_events(new_snapshots)
        if coroom_events:
            coroom_events = self._filter_coroom_events_by_interval(coroom_events)
            events.extend(coroom_events)
        logger.info(
            "[vrc_friend_radar] 本轮变化检测完成: status_tracking=%s, world_tracking=%s, raw_events=%s, filtered_events=%s, deduped_events=%s, coroom_events=%s, skipped_status=%s, skipped_world=%s",
            self.cfg.enable_status_tracking,
            self.cfg.enable_world_tracking,
            len(raw_events),
            len(filtered_events),
            len(events),
            len(coroom_events),
            skipped_status_events,
            skipped_world_events,
        )
        self.db.insert_event_history(events)
        self._last_sync_count = len(new_snapshots)
        self._last_detected_events = events
        return events

    def _build_coroom_events(self, snapshots: list[FriendSnapshot]) -> list[RadarEvent]:
        now = datetime.now().isoformat(timespec='seconds')
        allow = set(self.get_monitor_watch_friend_ids())
        if not allow:
            return []
        grouped: dict[str, list[FriendSnapshot]] = {}
        for item in snapshots:
            if item.friend_user_id not in allow:
                continue
            if (item.status or '').strip().lower() == 'offline':
                continue
            location_key = get_location_group_key(item.location)
            if not location_key:
                continue
            grouped.setdefault(location_key, []).append(item)

        events: list[RadarEvent] = []
        active_location_keys: list[str] = []
        min_members = self.cfg.coroom_notify_min_members
        joinable_only = self.cfg.coroom_notify_joinable_only
        for location_key, members in grouped.items():
            if len(members) < min_members:
                continue
            if joinable_only and infer_joinability(location_key) != '可加入':
                continue
            members.sort(key=lambda x: x.friend_user_id)
            signature = '|'.join(item.friend_user_id for item in members)
            old_signature = self.db.get_coroom_signature(location_key)
            self.db.set_coroom_signature(location_key, signature, now)
            active_location_keys.append(location_key)
            if old_signature == signature:
                continue
            display_names = '、'.join(sorted(item.display_name for item in members))
            events.append(
                RadarEvent(
                    friend_user_id=location_key,
                    display_name=display_names,
                    event_type='co_room',
                    old_value=old_signature,
                    new_value=signature,
                    created_at=now,
                )
            )

        self.db.delete_coroom_state_except(active_location_keys)
        return events

    def _filter_coroom_events_by_interval(self, events: list[RadarEvent]) -> list[RadarEvent]:
        now_ts = time.time()
        min_interval = self.cfg.coroom_notify_interval_seconds
        result: list[RadarEvent] = []
        active_keys = set()
        for event in events:
            location_key = event.friend_user_id
            active_keys.add(location_key)
            last_ts = self._last_coroom_notify_at.get(location_key, 0.0)
            if now_ts - last_ts < min_interval:
                continue
            self._last_coroom_notify_at[location_key] = now_ts
            result.append(event)
        stale_keys = [
            key
            for key, ts in self._last_coroom_notify_at.items()
            if key not in active_keys and (now_ts - ts) > min_interval
        ]
        for key in stale_keys:
            self._last_coroom_notify_at.pop(key, None)
        return result

    def _dedupe_events(self, events: list[RadarEvent]) -> list[RadarEvent]:
        lower_bound = (datetime.now() - timedelta(seconds=self.cfg.event_dedupe_window_seconds)).isoformat(timespec='seconds')
        seen_in_batch: set[str] = set()
        result: list[RadarEvent] = []
        for event in events:
            dedupe_key = f"{event.friend_user_id}:{event.event_type}:{event.old_value}:{event.new_value}"
            if dedupe_key in seen_in_batch:
                continue
            if self.db.event_exists_since(dedupe_key, lower_bound):
                continue
            seen_in_batch.add(dedupe_key)
            result.append(event)
        return result

    def build_event_messages(self, events: list[RadarEvent]) -> list[str]:
        messages = [self.notifier.build_message(event) for event in events]
        return messages[: self.cfg.event_batch_size]

    def list_cached_friends(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        return self.db.list_friend_snapshots(limit=limit, offset=offset)

    def list_online_cached_friends(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        return self.db.list_online_friend_snapshots(limit=limit, offset=offset)

    def list_coroom_groups(self, apply_query_filters: bool = True) -> list[dict]:
        watch_ids = self.get_monitor_watch_friend_ids()
        if not watch_ids:
            return []
        min_members = self.cfg.coroom_notify_min_members if apply_query_filters else 2
        return self.db.list_coroom_groups(watch_ids, min_members=min_members)

    def count_online_cached_friends(self) -> int:
        return self.db.count_online_friend_snapshots()

    def count_cached_friends(self) -> int:
        return self.db.count_friend_snapshots()

    def list_recent_events(self, limit: int = 20) -> list[RadarEvent]:
        watch_ids = self.get_monitor_watch_friend_ids()
        if not watch_ids:
            return []

        watch_set = set(watch_ids)
        raw_events = self.db.list_recent_events(limit=max(limit * 10, 100))
        filtered: list[RadarEvent] = []
        for item in raw_events:
            if item.event_type == 'co_room':
                member_ids = [x for x in (item.new_value or '').split('|') if x]
                # 同房事件要求成员全部位于监控名单，避免历史非监控数据泄露
                if member_ids and all(member_id in watch_set for member_id in member_ids):
                    filtered.append(item)
            else:
                if item.friend_user_id in watch_set:
                    filtered.append(item)
            if len(filtered) >= limit:
                break
        return filtered

    def create_pending_login(self, session_key: str, username: str, password: str, method: str) -> None:
        self._pending_logins[session_key] = PendingLoginSession(session_key=session_key, username=username, password=password, created_at=time.time(), method=method)

    def get_pending_login(self, session_key: str) -> PendingLoginSession | None:
        self._cleanup_pending_logins()
        return self._pending_logins.get(session_key)

    def pop_pending_login(self, session_key: str) -> PendingLoginSession | None:
        self._cleanup_pending_logins()
        return self._pending_logins.pop(session_key, None)

    def has_pending_login(self, session_key: str) -> bool:
        self._cleanup_pending_logins()
        return session_key in self._pending_logins

    def _cleanup_pending_logins(self) -> None:
        expired_keys = [key for key, session in self._pending_logins.items() if session.is_expired(self.cfg.login_session_timeout_seconds)]
        for key in expired_keys:
            self._pending_logins.pop(key, None)

    def get_runtime_summary(self) -> str:
        login_text = "未测试"
        if self._last_login_result is not None:
            login_text = f"成功({self._last_login_result.display_name})" if self._last_login_result.ok else f"失败({self._last_login_result.message})"
        return (
            "VRChat好友雷达运行中\n"
            f"轮询间隔: {self.cfg.poll_interval_seconds} 秒\n"
            f"监控好友数: {len(self.get_monitor_watch_friend_ids())}\n"
            f"通知群数: {len(self.get_effective_notify_groups())}\n"
            f"缓存好友数: {self.count_cached_friends()}\n"
            f"等待验证码会话数: {len(self._pending_logins)}\n"
            f"最近登录测试: {login_text}\n"
            f"最近同步好友数: {self._last_sync_count}\n"
            f"最近变化事件数: {len(self._last_detected_events)}\n"
            f"去重窗口: {self.cfg.event_dedupe_window_seconds} 秒\n"
            f"同房提醒间隔: {self.cfg.coroom_notify_interval_seconds} 秒\n"
            f"同房提醒人数阈值: {self.cfg.coroom_notify_min_members} 人\n"
            f"同房仅提醒可加入: {'开启' if self.cfg.coroom_notify_joinable_only else '关闭'}\n"
            f"每日任务默认时间: {self.cfg.daily_task_time}\n"
            f"日报推送: {'开启' if self.cfg.enable_daily_report else '关闭'} ({self.cfg.daily_report_time})\n"
            f"单次推送上限: {self.cfg.event_batch_size} 条\n"
            f"自动推送: {'开启' if self.cfg.allow_auto_push else '关闭'}\n"
            f"当前登录状态: {'已登录' if self.client.is_logged_in() else '未登录'}\n"
            f"轮询次数: {self._tick_count}"
        )
