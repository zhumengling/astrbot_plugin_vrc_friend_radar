"""每日定时任务调度 Mixin。

提供每日定时任务的调度逻辑，包括判断任务是否应执行、
记录上次发送日期、发送日报到通知群等，由 VRCFriendRadarPlugin 继承使用。
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

from .vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class DailySchedulerMixin:
    """每日定时任务调度 Mixin，self 即为插件实例。"""

    async def _handle_loop_tick(self: 'VRCFriendRadarPlugin', now: datetime) -> None:
        if self.cfg.enable_daily_report and self._daily_task_should_run('daily_report', now):
            sent = await self._send_daily_report_to_notify_groups(mark_sent=True)
            if sent > 0:
                logger.info(f"[vrc_friend_radar] 每日任务(daily_report)已发送，日期={now.strftime('%Y-%m-%d')}，群数量={sent}")

    def _daily_task_should_run(self: 'VRCFriendRadarPlugin', task_name: str, now: datetime) -> bool:
        today = now.strftime('%Y-%m-%d')
        if self._get_daily_task_last_sent_date(task_name) == today:
            return False
        task_time = self.cfg.get_daily_task_time(task_name)
        if now.strftime('%H:%M') < task_time:
            return False
        if not self.monitor.get_effective_notify_groups():
            return False
        return True

    def _get_daily_task_last_sent_date(self: 'VRCFriendRadarPlugin', task_name: str) -> str:
        return self._daily_task_last_sent_date.get(task_name, '')

    def _set_daily_task_last_sent_date(self: 'VRCFriendRadarPlugin', task_name: str, date_text: str) -> None:
        date_text = (date_text or '').strip()
        self._daily_task_last_sent_date[task_name] = date_text
        if task_name == 'daily_report':
            self.settings_repo.set_daily_report_last_sent_date(date_text)

    async def _send_daily_report_to_notify_groups(self: 'VRCFriendRadarPlugin', mark_sent: bool = True) -> int:
        try:
            components = await self._build_daily_report_components()
            success = await self._push_chain_to_notify_groups(components)
        except VRChatClientError as exc:
            logger.warning(f"[vrc_friend_radar] 日报构建或推送失败（认证/接口异常）: {exc}")
            return 0
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 日报构建或推送异常: {exc}", exc_info=True)
            return 0
        if success > 0 and mark_sent:
            today = datetime.now().strftime('%Y-%m-%d')
            self._set_daily_task_last_sent_date('daily_report', today)
        logger.info("[vrc_friend_radar] 日报推送完成: success_groups=%s mark_sent=%s", success, mark_sent)
        return success
