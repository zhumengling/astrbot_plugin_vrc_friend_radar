"""VRChat 站内通知同步服务。

负责周期性从 VRChat 拉取通知（好友请求/世界邀请/邀请求助等），
写入本地 vrc_notifications 表。插件层在此基础上提供 IM 端审批命令。
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from astrbot.api import logger

from .config import PluginConfig
from .db import RadarDB
from .vrchat_client import VRChatAuthInvalidError, VRChatClient, VRChatClientError


RELEVANT_NOTIFICATION_TYPES = {
    'friendRequest',
    'invite',
    'requestInvite',
    'invite.response',
    'requestInvite.response',
}


class NotificationSyncService:
    def __init__(self, cfg: PluginConfig, db: RadarDB, client_provider: Callable[[], VRChatClient]):
        self.cfg = cfg
        self.db = db
        self._client_provider = client_provider
        self._task: asyncio.Task | None = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._last_run_at: float = 0.0
        self._last_inserted_count: int = 0
        self._last_error: str = ''
        # 收到新通知时触发的回调。签名：async def(new_items: list[dict]) -> None
        self._callback: Callable[[list[dict]], Awaitable[None]] | None = None

    def set_callback(self, callback: Callable[[list[dict]], Awaitable[None]] | None) -> None:
        self._callback = callback

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
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

    async def fetch_once(self) -> list[dict]:
        """主动触发一次拉取，返回新增的通知列表。"""
        client = self._client_provider()
        if client is None or not client.is_logged_in():
            return []
        if not self.cfg.enable_notification_sync:
            return []

        try:
            items = await client.list_notifications(notification_type=None, hidden=False, n=60)
        except VRChatAuthInvalidError as exc:
            self._last_error = f'auth invalid: {exc}'
            logger.warning(f"[vrc_friend_radar][notify_sync] 拉取站内通知遇到认证失效: {exc}")
            return []
        except VRChatClientError as exc:
            self._last_error = str(exc)
            logger.warning(f"[vrc_friend_radar][notify_sync] 拉取站内通知失败: {exc}")
            return []
        except Exception as exc:  # 防御：保证同步循环不崩溃
            self._last_error = f'unexpected: {exc}'
            logger.error(f"[vrc_friend_radar][notify_sync] 拉取站内通知异常: {exc}", exc_info=True)
            return []

        filtered = [item for item in items if str(item.get('type') or '') in RELEVANT_NOTIFICATION_TYPES]
        # 找出新通知：保存前先查已有 ID
        existing_ids = {row['id'] for row in self.db.list_vrc_notifications(include_consumed=True, limit=500)}
        new_items = [item for item in filtered if str(item.get('id') or '') not in existing_ids]

        inserted = self.db.upsert_vrc_notifications(filtered)
        self._last_inserted_count = inserted
        self._last_run_at = time.time()
        self._last_error = ''
        # 顺手清理 14 天前的历史
        purged = self.db.purge_old_vrc_notifications(days=14)
        if purged:
            logger.info(f"[vrc_friend_radar][notify_sync] 清理过期站内通知 {purged} 条")

        if new_items and self._callback:
            try:
                await self._callback(new_items)
            except Exception as exc:
                logger.error(f"[vrc_friend_radar][notify_sync] 新通知回调执行失败: {exc}", exc_info=True)

        return new_items

    async def _run_loop(self) -> None:
        # 启动后稍等一会，避开与登录 restore 并发
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass

        while self._running:
            if self.cfg.enable_notification_sync:
                try:
                    await self.fetch_once()
                except Exception as exc:  # 防御
                    logger.error(f"[vrc_friend_radar][notify_sync] 循环内异常: {exc}", exc_info=True)
            interval = max(60, int(self.cfg.notification_sync_interval_seconds or 180))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    def get_status(self) -> dict:
        return {
            'running': self._running,
            'last_run_at': self._last_run_at,
            'last_inserted_count': self._last_inserted_count,
            'last_error': self._last_error,
            'interval': self.cfg.notification_sync_interval_seconds,
            'enabled': self.cfg.enable_notification_sync,
        }
