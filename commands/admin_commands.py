"""管理与标签命令 Mixin。"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.utils import format_location, infer_joinability
from ..core.vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class AdminCommandsMixin:
    """管理与标签命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc绑定通知群")
    async def bind_notify_group(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在群聊中使用。")
            return
        self.settings_repo.add_notify_group(group_id)
        groups, _ = self._sync_runtime_config_lists_from_repo()
        yield event.plain_result(f"已绑定通知群 {group_id}，当前通知群数量：{len(groups)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc解绑通知群")
    async def unbind_notify_group(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在群聊中使用。")
            return
        self.settings_repo.remove_notify_group(group_id)
        groups, _ = self._sync_runtime_config_lists_from_repo()
        yield event.plain_result(f"已解绑通知群 {group_id}，当前通知群数量：{len(groups)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc通知群")
    async def show_notify_groups(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        groups = self.monitor.get_effective_notify_groups()
        if not groups:
            yield event.plain_result("当前没有配置通知群。")
            return
        yield event.plain_result("通知群列表：\n" + "\n".join(groups))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc监控分组")
    async def tag_bind_group(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc监控分组", "", 1).strip()
        parts = raw.split()
        if len(parts) < 2:
            yield event.plain_result("用法：/vrc监控分组 tag 群号")
            return
        tag = parts[0].strip()
        group_id = parts[1].strip()
        if not tag or not group_id.isdigit():
            yield event.plain_result("tag 不能为空，群号必须为数字。")
            return
        self.monitor.add_tag_group_route(tag, group_id)
        routes = self.monitor.get_tag_group_routes()
        yield event.plain_result(
            f"已将 tag「{tag}」绑定到群 {group_id}，当前该 tag 的目标群：{'、'.join(routes.get(tag, []))}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc分组解绑")
    async def tag_unbind_group(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc分组解绑", "", 1).strip()
        parts = raw.split()
        if not parts:
            yield event.plain_result("用法：/vrc分组解绑 tag [群号]")
            return
        tag = parts[0].strip()
        group_id = parts[1].strip() if len(parts) >= 2 else None
        removed = self.monitor.remove_tag_group_route(tag, group_id)
        if removed <= 0:
            yield event.plain_result(f"未找到匹配的路由（tag={tag}, group={group_id or '任意'}）。")
            return
        yield event.plain_result(f"已解除 {removed} 条路由（tag={tag}, group={group_id or '全部'}）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc分组列表")
    async def tag_list(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        routes = self.monitor.get_tag_group_routes()
        tag_map = self.monitor.get_all_friend_tags()
        if not routes and not tag_map:
            yield event.plain_result("当前没有配置任何监控分组或好友 tag。")
            return
        lines: list[str] = []
        if routes:
            lines.append("分组→群 路由：")
            for tag, group_ids in sorted(routes.items(), key=lambda x: x[0].casefold()):
                lines.append(f"- {tag} → {'、'.join(group_ids) or '(无)'}")
        if tag_map:
            lines.append("")
            lines.append("好友 tag 映射（仅展示已打 tag 的）：")
            snapshot_map = self.db.get_friend_snapshot_map()
            for friend_id, tags in sorted(tag_map.items()):
                snapshot = snapshot_map.get(friend_id)
                display = self._sanitize_display_name_for_output(snapshot.display_name) if snapshot else friend_id
                lines.append(f"- {display}（{friend_id}）: {'、'.join(tags)}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc打标签")
    async def tag_friend(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc打标签", "", 1).strip()
        name_part, extras = self._split_name_and_extras(raw)
        if not name_part or not extras:
            yield event.plain_result("用法：/vrc打标签 名字或usr_xxx | tag1 tag2 ...")
            return
        try:
            friend_id, display_name = await self._resolve_profile_target_interactive(event, name_part, "打标签")
        except VRChatClientError as exc:
            yield event.plain_result(f"打标签失败：{exc}")
            return
        tags = [t for t in re.split(r"[\s,，、]+", extras) if t]
        cleaned = self.monitor.set_friend_tags(friend_id, tags)
        yield event.plain_result(f"已为 {display_name}（{friend_id}）打上 tag：{'、'.join(cleaned) if cleaned else '(无)'}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc隐私")
    async def toggle_group_privacy(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc隐私", "", 1).strip()
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在目标群内执行。")
            return
        if raw in {"不显示位置", "隐藏位置", "hide"}:
            self.monitor.set_group_privacy(group_id, True)
            yield event.plain_result(f"已将群 {group_id} 设置为不显示位置（事件中的世界/实例会被脱敏）。")
        elif raw in {"显示位置", "公开位置", "show"}:
            self.monitor.set_group_privacy(group_id, False)
            yield event.plain_result(f"已恢复群 {group_id} 的位置显示。")
        else:
            hide = group_id in self.monitor.get_hide_location_group_ids()
            current = "不显示位置" if hide else "显示位置"
            yield event.plain_result(f"当前群 {group_id} 隐私状态：{current}\n用法：/vrc隐私 不显示位置 | 显示位置")

    @filter.command("vrc签名订阅")
    async def subscribe_signature_keyword(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc签名订阅", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc签名订阅 关键词")
            return
        sender_id = str(event.get_sender_id() or '').strip()
        if not sender_id:
            yield event.plain_result("无法识别发送者 ID，订阅失败。")
            return
        self.db.add_signature_subscription(raw, sender_id)
        yield event.plain_result(f"已订阅签名关键词「{raw}」，命中时会私聊通知你。")

    @filter.command("vrc签名退订")
    async def unsubscribe_signature_keyword(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc签名退订", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc签名退订 关键词")
            return
        sender_id = str(event.get_sender_id() or '').strip()
        removed = self.db.remove_signature_subscription(raw, sender_id)
        if removed <= 0:
            yield event.plain_result(f"未找到你对关键词「{raw}」的订阅。")
            return
        yield event.plain_result(f"已退订签名关键词「{raw}」。")

    @filter.command("vrc签名订阅列表")
    async def list_signature_subscriptions(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        sender_id = str(event.get_sender_id() or '').strip()
        items = self.db.list_signature_subscriptions(subscriber_id=sender_id)
        if not items:
            yield event.plain_result("你当前没有签名关键词订阅。可用：/vrc签名订阅 关键词")
            return
        lines = ["你当前的签名关键词订阅："]
        for keyword, _sub in items:
            lines.append(f"- {keyword}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc自适应轮询")
    async def toggle_adaptive_polling(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc自适应轮询", "", 1).strip()
        if raw in {"开启", "on"}:
            self.cfg.enable_adaptive_polling = True
            if hasattr(self.cfg.raw_config, '__setitem__'):
                try:
                    self.cfg.raw_config['enable_adaptive_polling'] = True
                    if hasattr(self.cfg.raw_config, 'save_config'):
                        self.cfg.raw_config.save_config()
                except Exception as exc:
                    logger.warning(f"[vrc_friend_radar] 写回 enable_adaptive_polling 失败: {exc}")
            yield event.plain_result(
                f"已开启自适应轮询；范围 {self.cfg.adaptive_polling_min_seconds}-{self.cfg.adaptive_polling_max_seconds}s。"
            )
        elif raw in {"关闭", "off"}:
            self.cfg.enable_adaptive_polling = False
            if hasattr(self.cfg.raw_config, '__setitem__'):
                try:
                    self.cfg.raw_config['enable_adaptive_polling'] = False
                    if hasattr(self.cfg.raw_config, 'save_config'):
                        self.cfg.raw_config.save_config()
                except Exception as exc:
                    logger.warning(f"[vrc_friend_radar] 写回 enable_adaptive_polling 失败: {exc}")
            yield event.plain_result(f"已关闭自适应轮询，恢复固定 {self.cfg.poll_interval_seconds}s 间隔。")
        else:
            status = "开启" if getattr(self.cfg, 'enable_adaptive_polling', False) else "关闭"
            yield event.plain_result(
                f"当前自适应轮询：{status}（范围 {self.cfg.adaptive_polling_min_seconds}-{self.cfg.adaptive_polling_max_seconds}s）\n"
                "用法：/vrc自适应轮询 开启|关闭"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc状态")
    async def status(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        yield event.plain_result(self.monitor.get_runtime_summary())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc测试")
    async def test_notify(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        yield event.plain_result("VRChat好友雷达插件在线，测试消息发送正常。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc推送测试")
    async def push_test(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        await self._push_messages_to_notify_groups(["🧪 这是一条 VRChat 好友雷达自动推送测试消息。"])
        yield event.plain_result("已尝试向通知群发送测试消息。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc检测变化")
    async def detect_changes(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
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
    @filter.command("vrc同步好友")
    async def sync_friends(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
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
    @filter.command("vrc最近事件")
    async def recent_events(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        events = self.monitor.list_recent_events(limit=20)
        if not events:
            yield event.plain_result("当前没有事件历史。")
            return
        lines = ["最近事件："]
        for idx, item in enumerate(events, start=1):
            lines.append(f"{idx}. {item.event_type} | {item.friend_user_id} | {item.old_value or '空'} -> {item.new_value or '空'}")
        yield event.plain_result("\n".join(lines))

    @filter.command("vrc帮助")
    async def help_menu(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        sections = [
            "VRChat 好友雷达命令速查：",
            "【管理员】/vrc状态  /vrc测试  /vrc推送测试",
            "【管理员】/vrc登录 用户名 密码  /vrc验证码 123456  /vrc解绑登录",
            "【管理员】/vrc绑定通知群  /vrc解绑通知群  /vrc通知群",
            "【管理员】/vrc添加监控 名字或usr_xxx [| tag1 tag2 ...]  /vrc删除监控 名字或usr_xxx  /vrc监控列表",
            "【管理员】/vrc打标签 名字或usr_xxx | tag1 tag2 ...",
            "【管理员】/vrc监控分组 tag 群号  /vrc分组解绑 tag  /vrc分组列表",
            "【管理员】/vrc隐私 不显示位置/显示位置",
            "【管理员】/vrc签名订阅 关键词  /vrc签名退订 关键词  /vrc签名订阅列表",
            "【管理员】/vrc自适应轮询 开启|关闭",
            "【管理员】/vrc通知中心  /vrc通知审批 编号 同意|拒绝",
            "【管理员】/vrc接受邀请 编号  /vrc拒绝邀请 编号  /vrc邀请 名字或usr_xxx [| worldId:instanceId]",
            "【管理员】/vrc同步好友  /vrc好友列表  /vrc在线好友  /vrc同房情况",
            "【管理员】/vrc检测变化  /vrc最近事件  /vrc生成日报 [推送]  /vrc生成周报",
            "【管理员】/vrc导出事件 [天数]  /vrc公共加好友 开启|关闭",
            "【所有人】/vrc搜索好友 关键词 [页码]  /vrc搜索地图 关键词",
            "【所有人】/vrc热门世界 [N]  /vrc加好友 名字",
            "【所有人】/vrc戳 名字 | emojiId  /vrc资料 名字",
            "【所有人】/vrc灵魂画像 名字  /vrc人设 名字  /命运指引 名字  /vrc缘分 名字",
            "【所有人】/bili解析 BV号|av号|链接  /bili封面 BV号|链接",
        ]
        yield event.plain_result("\n".join(sections))
