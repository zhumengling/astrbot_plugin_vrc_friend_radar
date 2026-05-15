import asyncio
from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

# @register 装饰器兼容：某些 AstrBot 版本需要它来识别插件主类
try:
    from astrbot.api.star import register
except ImportError:
    def register(*args, **kwargs):
        """Fallback no-op: 当前 AstrBot 版本不需要 @register，纯 metadata.yaml 驱动。"""
        def decorator(cls):
            return cls
        return decorator

from .core.config import PluginConfig
from .core.db import RadarDB
from .core.monitor import MonitorService
from .core.repository import SearchRepository, SettingsRepository
from .core.search_state import SearchSession
from .core.world_cache import WorldCache
from .core.rendering import RenderingMixin
from .core.plugin_helpers import PluginHelpersMixin
from .core.event_dispatch import EventDispatchMixin
from .core.soul_profile import (
    SoulProfileMixin,
    SoulProfileSummary,
    ProfileTargetOption,
    ProfileTargetResolveResult,
)
from .core.daily_scheduler import DailySchedulerMixin
from .commands import (
    LoginCommandsMixin,
    BiliCommandsMixin,
    NotificationCommandsMixin,
    FriendCommandsMixin,
    SocialCommandsMixin,
    ReportCommandsMixin,
    AdminCommandsMixin,
    SoulProfileCommandsMixin,
)


def _rebind_handlers_to_module(cls, module_name: str) -> None:
    """将 Mixin 子模块中注册的 handler 在 star_handlers_registry 里的归属修正为插件主模块。

    AstrBot 4.24 的 StarHandlerRegistry 使用 handler_module_path 来关联 handler 与插件。
    当 handler 定义在 commands/*.py 的 Mixin 中时，handler_module_path 指向子模块路径，
    导致框架不把它们当成本插件的命令。此函数在类定义后遍历 registry 统一修正。
    """
    try:
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception as exc:
        logger.warning("[vrc_friend_radar] handler registry 兼容修正失败: %s", exc)
        return

    pkg_prefix = f"{__package__}.commands."
    core_prefix = f"{__package__}.core."
    rebound = 0

    for handler in list(star_handlers_registry):
        old_module = str(getattr(handler, "handler_module_path", "") or "")
        if not (old_module.startswith(pkg_prefix) or old_module.startswith(core_prefix)):
            continue

        old_full = str(getattr(handler, "handler_full_name", "") or "")
        new_full = f"{module_name}_{handler.handler_name}"

        star_handlers_registry.star_handlers_map.pop(old_full, None)
        handler.handler_module_path = module_name
        handler.handler_full_name = new_full
        star_handlers_registry.star_handlers_map[new_full] = handler
        rebound += 1

    if rebound:
        logger.info("[vrc_friend_radar] 已将 %s 个 Mixin 命令 handler 归属修正到 main.py", rebound)


@register(
    "astrbot_plugin_vrc_friend_radar",
    "zhumengling",
    "VRChat 好友上线/状态/地图切换监控与播报，支持邀请审批、灵魂画像、同房提醒等。",
    "0.2.4",
)
class VRCFriendRadarPlugin(
    Star,
    RenderingMixin,
    PluginHelpersMixin,
    EventDispatchMixin,
    SoulProfileMixin,
    DailySchedulerMixin,
    LoginCommandsMixin,
    BiliCommandsMixin,
    NotificationCommandsMixin,
    FriendCommandsMixin,
    SocialCommandsMixin,
    ReportCommandsMixin,
    AdminCommandsMixin,
    SoulProfileCommandsMixin,
):
    """VRChat 好友雷达插件主类"""

    # ---- AstrBot 4.24 兼容：显式将 Mixin 中的 @filter handler 绑定到本类 __dict__，
    # 使框架 handler 扫描器能发现它们（框架只扫描插件入口模块定义的方法）。----
    # Login
    interactive_login = LoginCommandsMixin.interactive_login
    submit_code = LoginCommandsMixin.submit_code
    clear_login = LoginCommandsMixin.clear_login
    # Friend
    add_watch_friend = FriendCommandsMixin.add_watch_friend
    remove_watch_friend = FriendCommandsMixin.remove_watch_friend
    show_watch_list = FriendCommandsMixin.show_watch_list
    search_friends = FriendCommandsMixin.search_friends
    friend_list = FriendCommandsMixin.friend_list
    online_friend_list = FriendCommandsMixin.online_friend_list
    add_watch_by_index = FriendCommandsMixin.add_watch_by_index
    search_worlds = FriendCommandsMixin.search_worlds
    global_search_users = FriendCommandsMixin.global_search_users
    export_friends = FriendCommandsMixin.export_friends
    # Social
    boop_friend = SocialCommandsMixin.boop_friend
    invite_to_instance = SocialCommandsMixin.invite_to_instance
    public_friend_request = SocialCommandsMixin.public_friend_request
    toggle_public_friend_request = SocialCommandsMixin.toggle_public_friend_request
    user_profile = SocialCommandsMixin.user_profile
    friendship_history = SocialCommandsMixin.friendship_history
    friend_note_set = SocialCommandsMixin.friend_note_set
    friend_note_list = SocialCommandsMixin.friend_note_list
    instance_info = SocialCommandsMixin.instance_info
    server_status = SocialCommandsMixin.server_status
    coroom_status = SocialCommandsMixin.coroom_status
    # Notification
    notification_center = NotificationCommandsMixin.notification_center
    approve_notification = NotificationCommandsMixin.approve_notification
    accept_invite = NotificationCommandsMixin.accept_invite
    reject_invite = NotificationCommandsMixin.reject_invite
    # Report
    generate_daily_report = ReportCommandsMixin.generate_daily_report
    weekly_report = ReportCommandsMixin.weekly_report
    hot_worlds = ReportCommandsMixin.hot_worlds
    export_events = ReportCommandsMixin.export_events
    activity_heatmap = ReportCommandsMixin.activity_heatmap
    # Admin
    bind_notify_group = AdminCommandsMixin.bind_notify_group
    unbind_notify_group = AdminCommandsMixin.unbind_notify_group
    show_notify_groups = AdminCommandsMixin.show_notify_groups
    tag_bind_group = AdminCommandsMixin.tag_bind_group
    tag_unbind_group = AdminCommandsMixin.tag_unbind_group
    tag_list = AdminCommandsMixin.tag_list
    tag_friend = AdminCommandsMixin.tag_friend
    toggle_group_privacy = AdminCommandsMixin.toggle_group_privacy
    subscribe_signature_keyword = AdminCommandsMixin.subscribe_signature_keyword
    unsubscribe_signature_keyword = AdminCommandsMixin.unsubscribe_signature_keyword
    list_signature_subscriptions = AdminCommandsMixin.list_signature_subscriptions
    toggle_adaptive_polling = AdminCommandsMixin.toggle_adaptive_polling
    status = AdminCommandsMixin.status
    test_notify = AdminCommandsMixin.test_notify
    push_test = AdminCommandsMixin.push_test
    detect_changes = AdminCommandsMixin.detect_changes
    sync_friends = AdminCommandsMixin.sync_friends
    recent_events = AdminCommandsMixin.recent_events
    help_menu = AdminCommandsMixin.help_menu
    # Soul Profile
    weekly_soul_profile = SoulProfileCommandsMixin.weekly_soul_profile
    persona_only = SoulProfileCommandsMixin.persona_only
    fortune_only = SoulProfileCommandsMixin.fortune_only
    relationship_score = SoulProfileCommandsMixin.relationship_score
    # Bili
    bili_parse_command = BiliCommandsMixin.bili_parse_command
    bili_cover_command = BiliCommandsMixin.bili_cover_command

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
        self.monitor.set_loop_tick_callback(self._handle_loop_tick)
        self.monitor.set_notice_callback(self._handle_monitor_notice)
        self.monitor.set_notification_sync_callback(self._handle_new_vrc_notifications)
        self._search_sessions: dict[str, SearchSession] = {}
        self._daily_task_last_sent_date: dict[str, str] = {"daily_report": ""}
        self._translation_lock_map: dict[str, asyncio.Lock] = {}
        self._last_private_admin_sender_id: str = ""

    def _reconcile_dynamic_lists_on_startup(self) -> tuple[list[str], list[str]]:
        config_notify_groups = self.cfg.read_notify_group_ids_from_raw()
        config_watch_friends = self.cfg.read_watch_friend_ids_from_raw()
        merged_notify_groups = self.settings_repo.sync_notify_groups_with_config(config_notify_groups)
        merged_watch_friends = self.settings_repo.sync_watch_friends_with_config(config_watch_friends)
        self.cfg.sync_runtime_lists(
            notify_group_ids=merged_notify_groups,
            watch_friend_ids=merged_watch_friends,
            write_back_raw=True,
        )
        return merged_notify_groups, merged_watch_friends

    def _sync_runtime_config_lists_from_repo(self) -> tuple[list[str], list[str]]:
        notify_groups = self.settings_repo.get_notify_groups()
        watch_friends = self.settings_repo.get_watch_friends()
        self.cfg.sync_runtime_lists(
            notify_group_ids=notify_groups,
            watch_friend_ids=watch_friends,
            write_back_raw=True,
        )
        return notify_groups, watch_friends

    async def initialize(self):
        self._search_sessions.clear()
        self._translation_lock_map.clear()
        self.db.initialize()
        self.settings_repo.initialize()
        merged_notify_groups, merged_watch_friends = self._reconcile_dynamic_lists_on_startup()
        self._daily_task_last_sent_date["daily_report"] = self.settings_repo.get_daily_report_last_sent_date()
        asyncio.create_task(self.monitor.start())
        self._register_llm_tools()
        logger.info(
            "[vrc_friend_radar] 插件后台初始化开始，已同步列表: notify_groups=%s, watch_friends=%s",
            len(merged_notify_groups),
            len(merged_watch_friends),
        )

    async def terminate(self):
        await self.monitor.stop()
        self._search_sessions.clear()
        self._translation_lock_map.clear()
        logger.info("[vrc_friend_radar] 插件已停止")

    def _register_llm_tools(self) -> None:
        """Register FunctionTool instances with AstrBot so the LLM agent can call them."""
        try:
            from .tools import build_llm_tools
            tools = build_llm_tools(self)
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 构建 LLM 工具失败，将跳过注册: {exc}")
            return

        add_method = getattr(self.context, 'add_llm_tools', None)
        if callable(add_method):
            try:
                add_method(*tools)
                logger.info(f"[vrc_friend_radar] 已注册 {len(tools)} 个 LLM FunctionTool")
                return
            except Exception as exc:
                logger.warning(f"[vrc_friend_radar] context.add_llm_tools 注册失败，尝试兼容路径: {exc}")

        # AstrBot < 4.5.1 的兼容路径
        try:
            tool_mgr = getattr(self.context, 'provider_manager', None)
            llm_tools = getattr(tool_mgr, 'llm_tools', None) if tool_mgr else None
            func_list = getattr(llm_tools, 'func_list', None) if llm_tools else None
            if isinstance(func_list, list):
                func_list.extend(tools)
                logger.info(f"[vrc_friend_radar] 已通过兼容路径注册 {len(tools)} 个 LLM FunctionTool")
                return
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] LLM 工具兼容路径注册失败: {exc}")

        logger.warning("[vrc_friend_radar] 当前 AstrBot 版本可能不支持 FunctionTool 注册，已跳过。")


# AstrBot handler 注册修正：将 Mixin 子模块中注册的 handler 归属修正到插件主模块，
# 使框架的 StarHandlerRegistry 能正确将它们关联到本插件。
_rebind_handlers_to_module(VRCFriendRadarPlugin, __name__)
