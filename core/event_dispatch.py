"""事件分发与消息推送 Mixin。

提供监控事件处理、消息推送到通知群/私聊用户、标签路由分发、
签名订阅分发等事件分发相关方法，由 VRCFriendRadarPlugin 继承使用。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools

from .utils import infer_joinability

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class EventDispatchMixin:
    """事件分发与消息推送 Mixin，self 即为插件实例。"""

    async def _handle_monitor_events(self: 'VRCFriendRadarPlugin', events) -> None:
        # 先尝试针对关键词订阅和 tag 路由做分发
        try:
            await self._dispatch_signature_subscriptions(events)
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 签名关键词订阅分发失败: {exc}")

        await self._dispatch_events_to_tag_routed_groups(events)

    async def _handle_monitor_notice(self: 'VRCFriendRadarPlugin', message: str) -> None:
        text = str(message or '').strip()
        if not text:
            return
        await self._push_login_notice_to_admins(text)

    async def _push_messages_to_notify_groups(self: 'VRCFriendRadarPlugin', messages: list[str]) -> None:
        if not messages:
            return
        merged = self.monitor.notifier.build_batch_message(
            messages[: self.cfg.event_batch_size]
        )
        await self._send_chain_to_groups(MessageChain([Plain(merged)]))

    async def _dispatch_events_to_tag_routed_groups(self: 'VRCFriendRadarPlugin', events) -> None:
        """根据监控好友的 tag 把事件路由到指定群；其余事件走默认通知群。

        群隐私开关：若群设置 hide_location=true，对该群的位置文本整体替换为「某个世界」，
        避免泄漏具体实例。
        """
        if not events:
            return
        event_list = list(events[: self.cfg.event_batch_size])
        default_groups = self.monitor.get_effective_notify_groups()

        # 按目标群聚合事件
        group_events: dict[str, list] = {}
        for item in event_list:
            # co_room 事件 friend_user_id 是 location_key，不好打 tag，落到默认群
            target_groups = default_groups
            if getattr(item, 'event_type', '') != 'co_room':
                target_groups = self.monitor.resolve_event_target_groups(item.friend_user_id, default_groups)
            for group_id in target_groups or []:
                group_events.setdefault(str(group_id), []).append(item)

        if not group_events:
            return

        hide_groups = self.monitor.get_hide_location_group_ids()
        for group_id, items in group_events.items():
            try:
                messages = await self._format_events_for_push(items)
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 为群 {group_id} 构建事件消息失败: {exc}", exc_info=True)
                continue
            if group_id in hide_groups:
                messages = [self._redact_location_detail(msg) for msg in messages]
            merged = self.monitor.notifier.build_batch_message(messages[: self.cfg.event_batch_size])
            if not merged:
                continue
            try:
                await StarTools.send_message_by_id(
                    type="GroupMessage",
                    id=group_id,
                    message_chain=MessageChain([Plain(merged)]),
                    platform="aiocqhttp",
                )
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 推送事件到群 {group_id} 失败: {exc}")

    async def _dispatch_signature_subscriptions(self: 'VRCFriendRadarPlugin', events) -> None:
        """当好友签名变化命中关键词订阅时，私聊订阅者。"""
        subs = self.db.list_signature_subscriptions()
        if not subs:
            return
        keyword_map: dict[str, list[str]] = {}
        for keyword, subscriber_id in subs:
            keyword_map.setdefault(keyword, []).append(subscriber_id)

        for event in events or []:
            if getattr(event, 'event_type', '') != 'status_message_changed':
                continue
            new_value = str(event.new_value or '')
            if not new_value:
                continue
            shown_name = self._sanitize_display_name_for_output(event.display_name)
            for keyword, subscribers in keyword_map.items():
                if keyword and keyword in new_value:
                    text = (
                        f"\U0001f514 签名关键词命中：{shown_name} 的 VRChat 状态签名包含「{keyword}」\n"
                        f"新签名：{new_value}"
                    )
                    await self._send_chain_to_private_users(subscribers, MessageChain([Plain(text)]))

    async def _format_events_for_push(self: 'VRCFriendRadarPlugin', events):
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
                names = [name for name in (item.display_name or '').split('\u3001') if name]
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
                messages.append(f"\u26ab {shown_name} 下线了")
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
                        f"状态变化：{_clean_text(status_change_event.old_value, 'unknown')} \u2192 {_clean_text(status_change_event.new_value, 'unknown')}"
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
                            f"切换地图：{old_name}（{old_joinability}） \u2192 {new_name}（{new_joinability}）"
                        )

                sign_event = next((item for item in items if item.event_type == 'status_message_changed'), None)
                if sign_event:
                    detail_lines.append(
                        f"签名：{_clean_text(sign_event.old_value)} \u2192 {_clean_text(sign_event.new_value)}"
                    )

                messages.append(_build_multiline_message(f"\U0001f7e2 {shown_name} 上线了", detail_lines))
                continue

            detail_lines = []
            status_change_event = next((item for item in items if item.event_type == 'status_changed'), None)
            if status_change_event:
                detail_lines.append(
                    f"状态变化：{_clean_text(status_change_event.old_value, 'unknown')} \u2192 {_clean_text(status_change_event.new_value, 'unknown')}"
                )

            location_event = next((item for item in items if item.event_type == 'location_changed'), None)
            if location_event:
                old_name = await self._get_world_name(location_event.old_value)
                new_name = await self._get_world_name(location_event.new_value)
                current_status = current.status if current else None
                old_joinability = infer_joinability(location_event.old_value)
                new_joinability = infer_joinability(location_event.new_value, status=current_status)
                detail_lines.append(
                    f"切换地图：{old_name}（{old_joinability}） \u2192 {new_name}（{new_joinability}）"
                )

            sign_event = next((item for item in items if item.event_type == 'status_message_changed'), None)
            if sign_event:
                detail_lines.append(
                    f"签名：{_clean_text(sign_event.old_value)} \u2192 {_clean_text(sign_event.new_value)}"
                )

            if detail_lines:
                messages.append(_build_multiline_message(f"\U0001f504 {shown_name}", detail_lines))
            else:
                for item in items:
                    messages.append(self.monitor.notifier.build_message(item))
        return messages

    async def _push_chain_to_notify_groups(self: 'VRCFriendRadarPlugin', components: list) -> int:
        chain = MessageChain(components)
        return await self._send_chain_to_groups(chain)

    async def _send_chain_to_groups(self: 'VRCFriendRadarPlugin', chain: MessageChain) -> int:
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

    async def _send_chain_to_private_users(self: 'VRCFriendRadarPlugin', user_ids: list[str], chain: MessageChain) -> int:
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
                logger.error(f"[vrc_friend_radar] 私聊发送失败 user={user_id}: {exc}")
        return success

    async def _push_login_notice_to_admins(self: 'VRCFriendRadarPlugin', message: str) -> None:
        text = str(message or '').strip()
        if not text:
            return
        targets = self._resolve_admin_notice_targets()
        if not targets:
            logger.warning('[vrc_friend_radar] 登录相关告警未发送：未获取到管理员ID（admins_id为空，且无私聊后备目标）。告警内容：%s', text)
            return
        success = await self._send_chain_to_private_users(targets, MessageChain([Plain(text)]))
        logger.info("[vrc_friend_radar] 登录告警已投递 admins=%s success=%s", len(targets), success)

    @staticmethod
    def _redact_location_detail(text: str) -> str:
        """粗粒度隐私脱敏：把具体世界/实例文字替换为「某个世界」。"""
        if not text:
            return text
        redacted = re.sub(r'（[^）]*实例[^）]*）', '', text)
        redacted = re.sub(r'位置：[^\n]+', '位置：某个世界', redacted)
        redacted = re.sub(r'切换地图：[^\n]+', '切换地图：某个世界 → 某个世界', redacted)
        return redacted

    async def _handle_new_vrc_notifications(self: 'VRCFriendRadarPlugin', notifications: list[dict]) -> None:
        """收到新的 VRChat 站内通知时，向管理员/通知群发送一条摘要提示。"""
        if not notifications:
            return
        lines = ["\U0001f4ec 收到新的 VRChat 站内通知，可执行 /vrc通知中心 审批："]
        for idx, item in enumerate(notifications[:5], start=1):
            notif_type = item.get('type') or 'unknown'
            sender = item.get('sender_username') or item.get('sender_user_id') or '未知'
            message = item.get('message') or ''
            lines.append(f"{idx}. [{notif_type}] 来自 {sender} | {message[:40]}")
        text = "\n".join(lines)
        admins = self._resolve_admin_notice_targets()
        if admins:
            await self._send_chain_to_private_users(admins, MessageChain([Plain(text)]))
        else:
            await self._send_chain_to_groups(MessageChain([Plain(text)]))
