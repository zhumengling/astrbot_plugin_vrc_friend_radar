import math
import tempfile
import uuid
import asyncio
from pathlib import Path
import time
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools

from .core.config import PluginConfig
from .core.db import RadarDB
from .core.monitor import MonitorService
from .core.repository import SearchRepository, SettingsRepository
from .core.search_state import SearchSession
from .core.utils import extract_world_id, format_location
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
        self._search_sessions: dict[str, SearchSession] = {}

    async def initialize(self):
        self.db.initialize()
        self.settings_repo.initialize()
        await self.monitor.start()
        logger.info("[vrc_friend_radar] 插件初始化完成")

    async def terminate(self):
        await self.monitor.stop()
        self._search_sessions.clear()
        logger.info("[vrc_friend_radar] 插件已停止")

    async def _handle_monitor_events(self, events) -> None:
        messages = await self._format_events_for_push(events)
        await self._push_messages_to_notify_groups(messages)

    async def _push_messages_to_notify_groups(self, messages: list[str]) -> None:
        if not messages:
            return
        groups = self.monitor.get_effective_notify_groups()
        if not groups:
            return
        merged = self.monitor.notifier.build_batch_message(messages[: self.cfg.event_batch_size])
        chain = MessageChain([Plain(merged)])
        for group_id in groups:
            try:
                await StarTools.send_message_by_id(type="GroupMessage", id=str(group_id), message_chain=chain, platform="aiocqhttp")
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 推送到群 {group_id} 失败: {exc}")

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
            return cached['name']
        info = await self.monitor.client.get_world_info(world_id)
        if info and info.get('name'):
            self.world_cache.set(world_id, info)
            return info['name']
        return '某个世界'

    async def _format_events_for_push(self, events):
        messages = []
        for item in events[: self.cfg.event_batch_size]:
            if item.event_type == 'location_changed':
                old_name = await self._get_world_name(item.old_value)
                new_name = await self._get_world_name(item.new_value)
                messages.append(f"🗺️ {item.display_name} 切换地图：{old_name} → {new_name}")
            else:
                messages.append(self.monitor.notifier.build_message(item))
        return messages


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
        groups = self.settings_repo.add_notify_group(group_id)
        yield event.plain_result(f"已绑定通知群 {group_id}，当前通知群数量：{len(groups)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc解绑通知群")
    async def unbind_notify_group(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在群聊中使用。")
            return
        groups = self.settings_repo.remove_notify_group(group_id)
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
        items = self.settings_repo.add_watch_friend(friend_id)
        yield event.plain_result(f"已添加监控好友 {friend_id}，当前监控数量：{len(items)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc删除监控")
    async def remove_watch_friend(self, event: AiocqhttpMessageEvent):
        friend_id = event.message_str.replace("vrc删除监控", "", 1).strip()
        if not friend_id:
            yield event.plain_result("用法：/vrc删除监控 usr_xxx")
            return
        items = self.settings_repo.remove_watch_friend(friend_id)
        yield event.plain_result(f"已删除监控好友 {friend_id}，当前监控数量：{len(items)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc监控列表")
    async def show_watch_list(self, event: AiocqhttpMessageEvent):
        items = self.monitor.get_effective_watch_friends()
        if not items:
            yield event.plain_result("当前没有配置监控好友，默认会同步全部好友。")
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
            lines.append(f"{idx}. {item.display_name} | ID: {item.friend_user_id} | 状态: {item.status or 'unknown'} | 位置: {format_location(item.location)}")
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
        items = self.settings_repo.add_watch_friend(target.friend_user_id)
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
            world_name = await self._get_world_name(item.location)
            lines.append(f"{idx}. {item.display_name} | 状态: {item.status or 'unknown'} | 地图: {world_name}")
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
            world_name = await self._get_world_name(item.location)
            lines.append(f"{idx}. {item.display_name} | 状态: {item.status or 'unknown'} | 地图: {world_name}")
        if page < total_pages:
            lines.append(f"下一页可用：/vrc好友列表 {page + 1}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc在线好友")
    async def online_friend_list(self, event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc在线好友", "", 1).strip()
        page = int(raw) if raw.isdigit() else 1
        page = max(1, page)
        page_size = 20
        total_online = self.monitor.count_online_cached_friends()
        if total_online <= 0:
            yield event.plain_result("当前缓存中没有在线好友，请先执行：/vrc同步好友")
            return
        snapshots = self.monitor.list_online_cached_friends(limit=page_size, offset=(page - 1) * page_size)
        total_pages = max(1, math.ceil(total_online / page_size))
        lines = [f"当前在线好友总数 {total_online} 人，当前第 {page}/{total_pages} 页："]
        for idx, item in enumerate(snapshots, start=1):
            world_name = await self._get_world_name(item.location)
            lines.append(f"{idx}. {item.display_name} | 状态: {item.status or 'unknown'} | 地图: {world_name}")
        if page < total_pages:
            lines.append(f"下一页可用：/vrc在线好友 {page + 1}")
        yield event.plain_result("\n".join(lines))

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
        messages = []
        for item in events[: self.cfg.event_batch_size]:
            if item.event_type == 'location_changed':
                old_name = await self._get_world_name(item.old_value)
                new_name = await self._get_world_name(item.new_value)
                messages.append(f"🗺️ {item.display_name} 切换地图：{old_name} → {new_name}")
            else:
                messages.append(self.monitor.notifier.build_message(item))
        yield event.plain_result(self.monitor.notifier.build_batch_message(messages))

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
