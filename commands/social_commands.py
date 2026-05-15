"""社交互动命令 Mixin。"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.utils import infer_joinability
from ..core.vrchat_errors import VRChatClientError, VRChatRateLimitedError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class SocialCommandsMixin:
    """社交互动命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    @filter.command("vrc戳")
    async def boop_friend(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc戳", "", 1).strip()
        if not raw:
            yield event.plain_result(
                "用法：/vrc戳 名字或usr_xxx | emojiId\n"
                "VRChat 的 Boop 只能携带一个 emoji，没有文字留言。\n"
                "emojiId 可选：留空=纯戳（对方只收到'被戳'通知）；\n"
                "   也可以填官方默认 emoji 的常量名（如 smile / skull / ghost），\n"
                "   或上传后的自定义贴纸 FileID。"
            )
            return
        name_part, emoji_id = self._split_name_and_extras(raw)
        if not name_part:
            yield event.plain_result("用法：/vrc戳 名字或usr_xxx | emojiId（emojiId 可留空）")
            return
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("当前未登录 VRChat，无法戳一戳。")
            return
        try:
            target, display_name = await self._resolve_profile_target_interactive(event, name_part, "戳一戳")
        except VRChatClientError as exc:
            yield event.plain_result(f"戳一戳失败：{exc}")
            return
        try:
            await self.monitor.client.boop_user(target, emoji_id or None)
        except VRChatRateLimitedError as exc:
            wait = exc.retry_after_seconds or 60
            yield event.plain_result(
                f"稍等一下再戳～{display_name} 的 Boop 正在冷却中（约 {wait} 秒）。"
                f"VRChat 对同一好友的 Boop 有服务端冷却窗口。"
            )
            return
        except VRChatClientError as exc:
            yield event.plain_result(f"戳一戳失败：{exc}")
            return
        if emoji_id:
            suffix = f"，带上了 emoji：{emoji_id}"
        else:
            suffix = "（纯戳，没带 emoji）"
        yield event.plain_result(f"已经戳了 {display_name}（{target}）一下{suffix}，说不定对方等会就回戳你～")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc邀请")
    async def invite_to_instance(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc邀请", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc邀请 名字或usr_xxx [| worldId:instanceId]")
            return
        name_part, instance_id = self._split_name_and_extras(raw)
        if not name_part:
            yield event.plain_result("用法：/vrc邀请 名字或usr_xxx [| worldId:instanceId]")
            return
        try:
            target, display_name = await self._resolve_profile_target_interactive(event, name_part, "发送实例邀请")
        except VRChatClientError as exc:
            yield event.plain_result(f"发送邀请失败：{exc}")
            return

        if not instance_id:
            # 默认：邀请到机器人当前所在实例
            try:
                self_snapshot = await self.monitor.client.fetch_self_snapshot()
            except Exception:
                self_snapshot = None
            instance_id = self_snapshot.location if self_snapshot else ''
            if not instance_id:
                yield event.plain_result("无法识别机器人当前实例，请显式传入：/vrc邀请 名字 | worldId:instanceId")
                return
        try:
            ok = await self.monitor.client.invite_user_to_instance(target, instance_id)
        except VRChatClientError as exc:
            yield event.plain_result(f"发送邀请失败：{exc}")
            return
        yield event.plain_result(f"已向 {display_name}（{target}）发送邀请，期待对方前来～" if ok else "邀请发送失败，请检查 VRChat 端状态。")

    @filter.command("vrc加好友")
    async def public_friend_request(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc加好友", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc加好友 名字或 usr_xxx")
            return
        if not self._is_public_friend_request_allowed():
            yield event.plain_result("当前管理员还没有开放公共加好友功能。")
            return
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("机器人当前还没有登录 VRChat 账号，暂时无法代发好友申请。")
            return
        try:
            target, display_name = await self._resolve_profile_target_interactive(event, raw, "发送好友申请")
        except VRChatClientError as exc:
            yield event.plain_result(f"发送好友申请失败：{exc}")
            return
        try:
            await self.monitor.client.send_friend_request(target)
            yield event.plain_result(f"已经帮你向 {display_name}（{target}）发出好友申请啦，接下来就温柔地等对方在 VRChat 里回应吧。")
        except VRChatClientError as exc:
            yield event.plain_result(f"发送好友申请失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc公共加好友")
    async def toggle_public_friend_request(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc公共加好友", "", 1).strip()
        if raw not in {"开启", "关闭"}:
            current = "开启" if self._is_public_friend_request_allowed() else "关闭"
            yield event.plain_result(f"当前公共加好友状态：{current}\n用法：/vrc公共加好友 开启 或 /vrc公共加好友 关闭")
            return
        enabled = raw == "开启"
        self._set_public_friend_request_allowed(enabled)
        yield event.plain_result(f"公共加好友功能已{raw}。")

    @filter.command("vrc资料")
    async def user_profile(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc资料", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc资料 名字或usr_xxx")
            return
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("当前未登录 VRChat，无法拉取用户资料。")
            return
        try:
            target, display_name = await self._resolve_profile_target_interactive(event, raw, "查看资料")
        except VRChatClientError as exc:
            yield event.plain_result(f"查看资料失败：{exc}")
            return
        try:
            info = await self.monitor.client.get_user_detail(target)
        except VRChatClientError as exc:
            yield event.plain_result(f"查看资料失败：{exc}")
            return
        if not info:
            yield event.plain_result(f"暂时无法获取 {display_name} 的公开资料。")
            return

        lines = [f"📇 {info.get('display_name') or display_name} 的 VRChat 资料"]
        lines.append(f"用户 ID：{info.get('id') or target}")
        if info.get('status'):
            parts = [f"状态：{info['status']}"]
            if info.get('status_description'):
                parts.append(f"签名：{info['status_description']}")
            lines.append(" | ".join(parts))
        if info.get('location'):
            world_text = await self._format_world_display(info['location'])
            lines.append(f"当前位置：{world_text}")
        if info.get('last_platform'):
            lines.append(f"最近平台：{info['last_platform']}")
        if info.get('last_login'):
            lines.append(f"最近登录：{info['last_login']}")
        if info.get('date_joined'):
            lines.append(f"加入日期：{info['date_joined']}")
        if info.get('is_friend'):
            lines.append("与当前账号互为好友")
        if info.get('bio'):
            bio = info['bio'].strip()
            if len(bio) > 180:
                bio = bio[:180] + '…'
            lines.append(f"简介：{bio}")
        if info.get('bio_links'):
            lines.append("外链：" + "、".join(info['bio_links'][:5]))
        if info.get('tags'):
            friendly_tags = [tag for tag in info['tags'] if not tag.startswith('system_')][:8]
            if friendly_tags:
                lines.append("标签：" + "、".join(friendly_tags))

        components: list = [Plain("\n".join(lines))]
        avatar_url = info.get('current_avatar_thumbnail_image_url') or info.get('current_avatar_image_url') or info.get('user_icon') or info.get('profile_pic_override')
        if avatar_url:
            local_img = await self._download_image_to_temp(avatar_url)
            if local_img:
                try:
                    components.append(Image.fromFileSystem(local_img))
                except Exception as exc:
                    logger.warning(f"[vrc_friend_radar] 用户资料图片拼接失败: {exc}")
        yield event.chain_result(components)

    @filter.command("vrc履历")
    async def friendship_history(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc履历", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc履历 名字或usr_xxx")
            return
        try:
            friend_id, display_name = await self._resolve_profile_target_interactive(event, raw, "查看履历")
        except VRChatClientError as exc:
            yield event.plain_result(f"查看履历失败：{exc}")
            return
        profile = self.db.get_friend_profile(friend_id)
        history = self.db.list_friend_name_history(friend_id, limit=10)
        note = self.db.get_friend_note(friend_id)
        snapshot = self.db.get_friend_snapshot_map().get(friend_id)
        tags = self.monitor.get_friend_tags(friend_id)

        lines = [f"📜 {display_name}（{friend_id}）的履历"]
        if profile and profile.get('first_seen_at'):
            first_seen = profile['first_seen_at']
            try:
                first_dt = datetime.fromisoformat(first_seen)
                days_known = max(0, (datetime.now() - first_dt).days)
                lines.append(f"初次发现：{first_seen}（认识约 {days_known} 天）")
            except Exception:
                lines.append(f"初次发现：{first_seen}")
        else:
            lines.append("初次发现：暂无记录（还没有同步过）")

        if tags:
            lines.append(f"标签：{'、'.join(tags)}")
        if note and note.get('note_text'):
            lines.append(f"备注：{note['note_text']}")
        if snapshot:
            status = snapshot.status or 'unknown'
            lines.append(f"当前状态：{status}")
            if snapshot.location and (snapshot.status or '').strip().lower() != 'offline':
                world_text = await self._format_world_display(snapshot.location)
                lines.append(f"当前位置：{world_text}")

        if history:
            lines.append("")
            lines.append("改名历史：")
            for item in history:
                old_name = item.get('old_display_name') or '(首次记录)'
                new_name = item.get('new_display_name')
                when = item.get('changed_at') or ''
                lines.append(f"- {when}: {old_name} → {new_name}")
        else:
            lines.append("改名历史：暂无记录")

        yield event.plain_result("\n".join(lines))

    @filter.command("vrc备注")
    async def friend_note_set(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc备注", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc备注 名字或usr_xxx | 备注文字（留空则清除备注）")
            return
        name_part, note_text = self._split_name_and_extras(raw)
        if not name_part:
            yield event.plain_result("用法：/vrc备注 名字或usr_xxx | 备注文字")
            return
        try:
            friend_id, display_name = await self._resolve_profile_target_interactive(event, name_part, "写本地备注")
        except VRChatClientError as exc:
            yield event.plain_result(f"写备注失败：{exc}")
            return
        self.db.set_friend_note(friend_id, note_text)
        # 同步写到 VRChat 账号（失败不影响本地备注）
        sync_hint = ''
        if self.monitor.client.is_logged_in():
            try:
                resp = await self.monitor.client.update_user_note(friend_id, note_text)
                if resp is not None:
                    sync_hint = '，已同步到 VRChat 账号'
            except VRChatClientError as exc:
                logger.warning(f"[vrc_friend_radar] update_user_note 同步失败: {exc}")
                sync_hint = '，VRChat 端同步失败（备注仍已在本地保存）'
        if not note_text:
            yield event.plain_result(f"已清除 {display_name} 的本地备注{sync_hint}。")
        else:
            yield event.plain_result(f"已为 {display_name} 写备注：{note_text}{sync_hint}")

    @filter.command("vrc备注列表")
    async def friend_note_list(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        notes = self.db.list_friend_notes()
        if not notes:
            yield event.plain_result("当前没有任何好友备注。可用 /vrc备注 名字 | 备注文字 新增。")
            return
        snapshot_map = self.db.get_friend_snapshot_map()
        lines = [f"共 {len(notes)} 条好友备注："]
        for idx, (friend_id, note) in enumerate(sorted(notes.items(), key=lambda x: x[0]), start=1):
            snap = snapshot_map.get(friend_id)
            display = self._sanitize_display_name_for_output(snap.display_name) if snap else friend_id
            lines.append(f"{idx}. {display}：{note}")
            if idx >= 30:
                lines.append(f"… 其余 {len(notes) - idx} 条未展示")
                break
        yield event.plain_result("\n".join(lines))

    @filter.command("vrc实例")
    async def instance_info(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        from ..core.utils import extract_world_id
        raw = event.message_str.replace("vrc实例", "", 1).strip()
        # 入参：可以是 location key (wrld_xxx:instance) / world URL / 名字 / 留空用我自己的位置
        world_id = ''
        instance_id = ''
        if raw:
            # worldId:instanceId 直给
            if raw.startswith('wrld_'):
                if ':' in raw:
                    world_part, instance_part = raw.split(':', 1)
                    world_id = world_part.strip()
                    instance_id = instance_part.split('~', 1)[0].strip()
                else:
                    # 只有世界 ID 没有实例 ID，报错友好提示
                    yield event.plain_result("缺少实例 ID，用法：/vrc实例 wrld_xxx:12345~public")
                    return
            else:
                # 作为好友名解析，然后读他的 snapshot.location
                try:
                    friend_id, _display_name = await self._resolve_profile_target_interactive(event, raw, "查看实例")
                except VRChatClientError as exc:
                    yield event.plain_result(f"查看实例失败：{exc}")
                    return
                snap = self.db.get_friend_snapshot_map().get(friend_id)
                if not snap or not snap.location:
                    yield event.plain_result("该好友当前没有可查询的实例信息（可能不在任何世界中）。")
                    return
                world_id = extract_world_id(snap.location)
                if snap.location and ':' in snap.location:
                    instance_id = snap.location.split(':', 1)[1].split('~', 1)[0].strip()
        else:
            # 用机器人自己的位置
            try:
                self_snap = await self.monitor.client.fetch_self_snapshot()
            except Exception:
                self_snap = None
            if not self_snap or not self_snap.location:
                yield event.plain_result("当前机器人不在任何实例中，用法：/vrc实例 wrld_xxx:12345~public 或 /vrc实例 好友名")
                return
            world_id = extract_world_id(self_snap.location)
            if self_snap.location and ':' in self_snap.location:
                instance_id = self_snap.location.split(':', 1)[1].split('~', 1)[0].strip()

        if not world_id or not instance_id:
            yield event.plain_result("没有解析出有效的 worldId + instanceId。")
            return

        try:
            info = await self.monitor.client.get_instance(world_id, instance_id)
        except VRChatClientError as exc:
            yield event.plain_result(f"查实例失败：{exc}")
            return
        if not info:
            yield event.plain_result("未能查到该实例（可能已经关闭或不存在）。")
            return

        world_name = await self._get_world_name(f"{world_id}:{instance_id}")
        lines = [f"🏠 {world_name}"]
        lines.append(f"实例 ID：{instance_id}")
        capacity = info.get('capacity') or 0
        n_users = info.get('n_users') or 0
        if capacity:
            lines.append(f"人数：{n_users}/{capacity}" + ("（已满）" if info.get('full') else ""))
        else:
            lines.append(f"人数：{n_users}")
        if info.get('region'):
            lines.append(f"区域：{info['region']}")
        if info.get('access_type'):
            lines.append(f"类型：{info['access_type']}")
        if info.get('owner_id'):
            lines.append(f"Owner：{info['owner_id']}")
        if info.get('closed_at'):
            lines.append(f"关闭时间：{info['closed_at']}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc服务状态")
    async def server_status(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("未登录 VRChat，无法查询服务状态。")
            return
        try:
            status = await self.monitor.client.get_server_status()
        except VRChatClientError as exc:
            yield event.plain_result(f"查询服务状态失败：{exc}")
            return
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 查询服务状态异常: {exc}")
            yield event.plain_result(f"查询服务状态异常：{exc}")
            return
        lines = ["🩺 VRChat 服务状态"]
        if status.get('server_time'):
            lines.append(f"服务器时间：{status['server_time']}")
        if status.get('online_count'):
            lines.append(f"当前全平台在线：约 {status['online_count']} 人")
        auto_recover = self.monitor.get_auto_recover_status()
        if auto_recover:
            lines.append(f"本插件会话：{auto_recover.get('last_result') or '未知'}")
        errors = status.get('errors') or []
        if errors:
            lines.append("诊断信息：")
            for item in errors[:5]:
                lines.append(f"- {item}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc同房情况")
    async def coroom_status(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
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
