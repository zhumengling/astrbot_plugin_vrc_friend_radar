"""好友管理命令 Mixin。"""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.utils import format_location, infer_joinability
from ..core.vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class FriendCommandsMixin:
    """好友管理命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc添加监控")
    async def add_watch_friend(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc添加监控", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc添加监控 名字或usr_xxx [| tag1 tag2 ...]")
            return
        name_part, extras = self._split_name_and_extras(raw)
        if not name_part:
            yield event.plain_result("用法：/vrc添加监控 名字或usr_xxx [| tag1 tag2 ...]")
            return
        try:
            friend_id, display_name = await self._resolve_profile_target_interactive(event, name_part, "添加监控")
        except VRChatClientError as exc:
            yield event.plain_result(f"添加监控失败：{exc}")
            return
        self.settings_repo.add_watch_friend(friend_id)
        _, items = self._sync_runtime_config_lists_from_repo()
        tag_text = ''
        tags = [t for t in re.split(r"[\s,，、]+", extras) if t] if extras else []
        if tags:
            cleaned = self.monitor.set_friend_tags(friend_id, tags)
            if cleaned:
                tag_text = f"，tag={'、'.join(cleaned)}"
        yield event.plain_result(f"已添加监控好友 {display_name}（{friend_id}）{tag_text}，当前监控数量：{len(items)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc删除监控")
    async def remove_watch_friend(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc删除监控", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc删除监控 名字或usr_xxx")
            return
        try:
            friend_id, display_name = await self._resolve_profile_target_interactive(event, raw, "删除监控")
        except VRChatClientError as exc:
            yield event.plain_result(f"删除监控失败：{exc}")
            return
        self.settings_repo.remove_watch_friend(friend_id)
        _, items = self._sync_runtime_config_lists_from_repo()
        yield event.plain_result(f"已删除监控好友 {display_name}（{friend_id}），当前监控数量：{len(items)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc监控列表")
    async def show_watch_list(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        items = self.monitor.get_effective_watch_friends()
        if not items:
            yield event.plain_result("当前监控好友列表为空。监控提醒/监控事件/同房提醒将不会产生；好友列表与搜索仍可查看全好友缓存。")
            return

        snapshot_map = self.db.get_friend_snapshot_map()
        tag_map = self.monitor.get_all_friend_tags()
        lines: list[str] = []
        for idx, friend_id in enumerate(items, start=1):
            snapshot = snapshot_map.get(friend_id)
            display_name = self._sanitize_display_name_for_output(snapshot.display_name if snapshot else '')
            shown_name = display_name or friend_id
            tags = tag_map.get(friend_id) or []
            tag_text = f" | tag: {'、'.join(tags)}" if tags else ''
            lines.append(f"{idx}. {shown_name}{tag_text}")

        yield event.plain_result("监控好友列表：\n" + "\n".join(lines))

    @filter.command("vrc搜索好友")
    async def search_friends(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
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
            shown_name = self._sanitize_display_name_for_output(item.display_name)
            lines.append(f"{idx}. {shown_name} | ID: {item.friend_user_id} | 状态: {item.status or 'unknown'} | 位置: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
        lines.append("可使用：/vrc添加监控序号 1")
        if total_pages > page:
            lines.append(f"下一页可用：/vrc搜索好友 {keyword} {page + 1}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc好友列表")
    async def friend_list(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
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
            shown_name = self._sanitize_display_name_for_output(item.display_name)
            lines.append(f"{idx}. {shown_name} | 状态: {item.status or 'unknown'} | 地图: {format_location(item.location)} | {infer_joinability(item.location, status=item.status)}")
        if page < total_pages:
            lines.append(f"下一页可用：/vrc好友列表 {page + 1}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc在线好友")
    async def online_friend_list(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc在线好友", "", 1).strip()
        page = int(raw) if raw.isdigit() else 1
        try:
            yield event.plain_result(await self._build_online_friend_list_message(page=page))
        except VRChatClientError as exc:
            yield event.plain_result(f"获取在线好友失败：{exc}")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 查询在线好友异常: {exc}")
            yield event.plain_result("获取在线好友时发生异常，请稍后重试。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc添加监控序号")
    async def add_watch_by_index(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
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
        shown_name = self._sanitize_display_name_for_output(target.display_name)
        yield event.plain_result(f"已添加监控好友：{shown_name} | {target.friend_user_id}，当前监控数量：{len(items)}")

    @filter.command("vrc搜索地图")
    async def search_worlds(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        keyword = event.message_str.replace("vrc搜索地图", "", 1).strip()
        if not keyword:
            yield event.plain_result("用法：/vrc搜索地图 关键词")
            return
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("当前未登录，无法搜索地图。")
            return
        try:
            results = await self.monitor.client.search_worlds(keyword, limit=5, offset=0)
        except VRChatClientError as exc:
            yield event.plain_result(f"搜索地图失败：{exc}")
            return
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 搜索地图异常: {exc}")
            yield event.plain_result("搜索地图时发生异常，请稍后重试。")
            return
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc全局搜好友")
    async def global_search_users(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc全局搜好友", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc全局搜好友 关键词")
            return
        if not self.monitor.client.is_logged_in():
            yield event.plain_result("当前未登录 VRChat，无法做站内全局搜索。")
            return
        try:
            results = await self.monitor.client.search_users(raw, limit=10, offset=0)
        except VRChatClientError as exc:
            yield event.plain_result(f"全局搜索失败：{exc}")
            return
        if not results:
            yield event.plain_result(f"没有搜索到与 \"{raw}\" 相关的用户。")
            return
        lines = [f"🔎 全局搜到 {len(results)} 位用户："]
        for idx, item in enumerate(results, start=1):
            name = item.get('display_name') or '(未命名)'
            uid = item.get('id') or ''
            status = item.get('status') or 'unknown'
            lines.append(f"{idx}. {name} | {uid} | 状态 {status}")
        lines.append("可用 /vrc资料 usr_xxx 查看更多信息，或 /vrc加好友 名字 发送好友请求。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc导出好友")
    async def export_friends(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        from datetime import datetime
        snapshots = self.monitor.list_cached_friends(limit=5000, offset=0)
        if not snapshots:
            yield event.plain_result("当前没有缓存好友。先执行 /vrc同步好友 再试。")
            return
        profiles_map: dict[str, dict] = {}
        for snap in snapshots:
            profile = self.db.get_friend_profile(snap.friend_user_id)
            if profile:
                profiles_map[snap.friend_user_id] = profile
        tag_map = self.monitor.get_all_friend_tags()
        notes_map = self.db.list_friend_notes()

        export_dir = self.cfg.data_dir / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = export_dir / f"friends_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        import csv
        try:
            with filename.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'friend_user_id', 'display_name', 'status', 'location',
                    'status_description', 'updated_at', 'first_seen_at',
                    'tags', 'note',
                ])
                for snap in snapshots:
                    profile = profiles_map.get(snap.friend_user_id) or {}
                    writer.writerow([
                        snap.friend_user_id,
                        snap.display_name,
                        snap.status or '',
                        snap.location or '',
                        snap.status_description or '',
                        snap.updated_at or '',
                        profile.get('first_seen_at') or '',
                        '、'.join(tag_map.get(snap.friend_user_id, [])),
                        notes_map.get(snap.friend_user_id, ''),
                    ])
        except Exception as exc:
            yield event.plain_result(f"导出失败：{exc}")
            return
        yield event.plain_result(f"已导出 {len(snapshots)} 位好友到：{filename}")
