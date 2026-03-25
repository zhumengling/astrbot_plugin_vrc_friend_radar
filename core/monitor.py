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

    def get_effective_notify_groups(self) -> list[str]:
        runtime_groups = self.settings_repo.get_notify_groups()
        if runtime_groups:
            return runtime_groups
        return list(self.cfg.notify_group_ids)

    def get_effective_watch_friends(self) -> list[str]:
        runtime_ids = self.settings_repo.get_watch_friends()
        if runtime_ids:
            return runtime_ids
        return list(self.cfg.watch_friend_ids)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.try_restore_session()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
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
                    events = await self.detect_changes()
                    if events and self._event_callback:
                        await self._event_callback(events)
                except Exception as exc:
                    logger.error(f"[vrc_friend_radar] 轮询检测失败: {exc}", exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_seconds)

    async def test_login(self, username: str, password: str, two_factor_code: str | None = None) -> LoginResult:
        result = await self.client.login(username=username, password=password, two_factor_code=two_factor_code)
        self._last_login_result = result
        self.persist_session()
        return result

    async def sync_friends(self) -> list[FriendSnapshot]:
        snapshots = await self.client.fetch_friend_snapshots(self.get_effective_watch_friends())
        self.db.upsert_friend_snapshots(snapshots)
        self._last_sync_count = len(snapshots)
        return snapshots

    def get_sync_debug(self) -> dict[str, int]:
        return self.client.get_last_sync_debug()

    async def detect_changes(self) -> list[RadarEvent]:
        old_map = self.db.get_friend_snapshot_map()
        new_snapshots = await self.client.fetch_friend_snapshots(self.get_effective_watch_friends())
        events: list[RadarEvent] = []
        for item in new_snapshots:
            old_item = old_map.get(item.friend_user_id)
            if old_item is None:
                continue
            events.extend(diff_snapshot(old_item, item))
        events = self._dedupe_events(events)
        self.db.upsert_friend_snapshots(new_snapshots)
        self.db.insert_event_history(events)
        self._last_sync_count = len(new_snapshots)
        self._last_detected_events = events
        return events

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

    def count_online_cached_friends(self) -> int:
        return self.db.count_online_friend_snapshots()

    def count_cached_friends(self) -> int:
        return self.db.count_friend_snapshots()

    def list_recent_events(self, limit: int = 20) -> list[RadarEvent]:
        return self.db.list_recent_events(limit=limit)

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
            f"监控好友数: {len(self.get_effective_watch_friends())}\n"
            f"通知群数: {len(self.get_effective_notify_groups())}\n"
            f"缓存好友数: {self.count_cached_friends()}\n"
            f"等待验证码会话数: {len(self._pending_logins)}\n"
            f"最近登录测试: {login_text}\n"
            f"最近同步好友数: {self._last_sync_count}\n"
            f"最近变化事件数: {len(self._last_detected_events)}\n"
            f"去重窗口: {self.cfg.event_dedupe_window_seconds} 秒\n"
            f"单次推送上限: {self.cfg.event_batch_size} 条\n"
            f"自动推送: {'开启' if self.cfg.allow_auto_push else '关闭'}\n"
            f"当前登录状态: {'已登录' if self.client.is_logged_in() else '未登录'}\n"
            f"轮询次数: {self._tick_count}"
        )
