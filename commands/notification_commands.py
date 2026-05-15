"""通知中心与审批命令 Mixin。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class NotificationCommandsMixin:
    """通知中心与审批命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc通知中心")
    async def notification_center(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        # 先触发一次同步（不阻塞过久）
        try:
            if hasattr(self.monitor, '_notification_sync_service'):
                await asyncio.wait_for(self.monitor._notification_sync_service.fetch_once(), timeout=8)
            else:
                await asyncio.wait_for(self.monitor._try_periodic_notification_sync(), timeout=8)
        except asyncio.TimeoutError:
            logger.warning("[vrc_friend_radar] 站内通知同步超时，使用本地缓存继续响应。")
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 站内通知主动同步失败: {exc}")

        items = self.db.list_vrc_notifications(include_consumed=False, limit=20)
        if not items:
            yield event.plain_result("当前没有待处理的 VRChat 站内通知。")
            return
        lines = [f"待处理 VRChat 站内通知共 {len(items)} 条："]
        for idx, item in enumerate(items, start=1):
            sender = item.get('sender_username') or item.get('sender_user_id') or '未知'
            notif_type = item.get('type') or 'unknown'
            message = (item.get('message') or '').strip()
            message_display = (message[:40] + '…') if len(message) > 40 else message
            lines.append(f"{idx}. [{notif_type}] 来自 {sender} | {message_display or '(无留言)'}")
        lines.append("")
        lines.append("处理方式：")
        lines.append("/vrc通知审批 编号 同意  /vrc通知审批 编号 拒绝")
        lines.append("/vrc接受邀请 编号  /vrc拒绝邀请 编号")
        yield event.plain_result("\n".join(lines))

    def _pick_pending_notification(self: 'VRCFriendRadarPlugin', index_text: str) -> dict | None:
        try:
            idx = int(index_text)
        except ValueError:
            return None
        items = self.db.list_vrc_notifications(include_consumed=False, limit=50)
        if idx < 1 or idx > len(items):
            return None
        return items[idx - 1]

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc通知审批")
    async def approve_notification(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc通知审批", "", 1).strip()
        parts = raw.split()
        if len(parts) < 2:
            yield event.plain_result("用法：/vrc通知审批 编号 同意|拒绝")
            return
        item = self._pick_pending_notification(parts[0])
        if item is None:
            yield event.plain_result("未找到对应编号的通知，请先执行 /vrc通知中心 确认。")
            return
        decision = parts[1].strip()
        if decision not in {"同意", "拒绝", "accept", "reject"}:
            yield event.plain_result("第二个参数必须是 同意 或 拒绝。")
            return
        accept = decision in {"同意", "accept"}
        try:
            ok = await self.monitor.client.respond_friend_request(item['id'], accept)
        except VRChatClientError as exc:
            yield event.plain_result(f"处理通知失败：{exc}")
            return
        if not ok:
            yield event.plain_result("处理通知失败，可能 SDK 未适配，请稍后重试。")
            return
        self.db.mark_vrc_notification_consumed(item['id'])
        yield event.plain_result(f"已{'同意' if accept else '拒绝'}该通知：{item.get('sender_username') or item.get('sender_user_id')}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc接受邀请")
    async def accept_invite(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc接受邀请", "", 1).strip()
        item = self._pick_pending_notification(raw)
        if item is None:
            yield event.plain_result("未找到对应编号的邀请，请先执行 /vrc通知中心。")
            return
        # 邀请通知通常包含 details.worldId 或 details.instanceId
        details = item.get('details') or {}
        world_id = str(details.get('worldId') or '').strip()
        instance_id = str(details.get('instanceId') or details.get('location') or '').strip()
        if not world_id or not instance_id:
            yield event.plain_result("该通知不包含可直接接受的实例信息，建议在 VRChat 客户端内处理。")
            return
        # 接受邀请在 VRChat API 中其实就是「删除通知」+「用户自行前往」；这里给出实例地址便于跳转
        self.db.mark_vrc_notification_consumed(item['id'])
        yield event.plain_result(
            f"已标记邀请为已处理。建议前往：worldId={world_id}, instanceId={instance_id}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc拒绝邀请")
    async def reject_invite(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc拒绝邀请", "", 1).strip()
        item = self._pick_pending_notification(raw)
        if item is None:
            yield event.plain_result("未找到对应编号的邀请，请先执行 /vrc通知中心。")
            return
        try:
            await self.monitor.client.mark_notification_seen(item['id'])
        except VRChatClientError as exc:
            logger.warning(f"[vrc_friend_radar] 标记邀请已读失败: {exc}")
        self.db.mark_vrc_notification_consumed(item['id'])
        yield event.plain_result("已拒绝该邀请并在本地标记为已处理。")
