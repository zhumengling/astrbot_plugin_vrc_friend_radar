# Tasks

## Task 1: 提取 VRChat 错误类模块 (vrchat_errors.py)

- [x] 1.1 创建 `core/vrchat_errors.py`，添加模块文档字符串说明其职责（VRChat API 错误类定义）
- [x] 1.2 从 `core/vrchat_client.py` 中移动所有错误类（VRChatClientError、VRChatTwoFactorRequiredError、VRChatAuthInvalidError、VRChatNetworkError、VRChatRateLimitedError）到新文件
- [x] 1.3 在 `core/vrchat_client.py` 中添加 `from core.vrchat_errors import ...` 导入语句，替换原有的类定义
- [x] 1.4 更新 `core/__init__.py` 添加向后兼容导出，确保外部代码仍可通过 `from core import VRChatClientError` 访问
- [x] 1.5 运行 `python -m py_compile core/vrchat_errors.py` 和 `python -m py_compile core/vrchat_client.py` 验证无语法错误

## Task 2: 提取 VRChat 认证模块 (vrchat_auth.py) [depends:1]

- [x] 2.1 创建 `core/vrchat_auth.py`，添加模块文档字符串（VRChat 认证与会话管理 Mixin）
- [x] 2.2 从 `core/vrchat_client.py` 中移动认证相关方法（_login_sync、restore_session、_restore_session_sync、export_session、_extract_cookie_header、probe_auth_token、_probe_auth_token_sync、probe_session_health、_probe_session_health_sync、verify_session_ready、_verify_session_ready_sync、is_logged_in、get_saved_credentials、close、_create_api_client、_request_timeout_tuple）到 `VRChatAuthMixin` 类
- [x] 2.3 在 `core/vrchat_auth.py` 中添加必要的导入（from core.vrchat_errors import ...，以及 TYPE_CHECKING 守卫下的类型引用）
- [x] 2.4 修改 `core/vrchat_client.py` 中的 `VRChatClient` 类，使其继承 `VRChatAuthMixin`
- [x] 2.5 运行 `python -m py_compile core/vrchat_auth.py` 和 `python -m py_compile core/vrchat_client.py` 验证无语法错误

## Task 3: 提取 VRChat 社交 API 模块 (vrchat_social.py) [depends:1]

- [x] 3.1 创建 `core/vrchat_social.py`，添加模块文档字符串（VRChat 社交 API Mixin）
- [x] 3.2 从 `core/vrchat_client.py` 中移动社交 API 方法（send_friend_request、respond_friend_request、boop_user、invite_user_to_instance、search_users、get_user_detail、update_user_note、list_user_groups、list_blocked_user_ids 及对应的 _*_sync 版本）到 `VRChatSocialMixin` 类
- [x] 3.3 在 `core/vrchat_social.py` 中添加必要的导入（from core.vrchat_errors import ...，from core.models import ...）
- [x] 3.4 修改 `core/vrchat_client.py` 中的 `VRChatClient` 类，使其继承 `VRChatSocialMixin`
- [x] 3.5 运行 `python -m py_compile core/vrchat_social.py` 和 `python -m py_compile core/vrchat_client.py` 验证无语法错误

## Task 4: 提取 VRChat 世界 API 模块 (vrchat_world.py) [depends:1]

- [x] 4.1 创建 `core/vrchat_world.py`，添加模块文档字符串（VRChat 世界与实例 API Mixin）
- [x] 4.2 从 `core/vrchat_client.py` 中移动世界/实例 API 方法（get_world_info、search_worlds、get_instance、list_favorite_worlds、get_server_status 及对应的 _*_sync 版本）到 `VRChatWorldMixin` 类
- [x] 4.3 在 `core/vrchat_world.py` 中添加必要的导入（from core.vrchat_errors import ...）
- [x] 4.4 修改 `core/vrchat_client.py` 中的 `VRChatClient` 类，使其继承 `VRChatWorldMixin`
- [x] 4.5 运行 `python -m py_compile core/vrchat_world.py` 和 `python -m py_compile core/vrchat_client.py` 验证无语法错误
- [x] 4.6 确认 `core/vrchat_client.py` 瘦身后约 500 行，低于 1000 行限制

## Task 5: 提取监控自动恢复模块 (monitor_recovery.py) [depends:2]

- [x] 5.1 创建 `core/monitor_recovery.py`，添加模块文档字符串（监控服务自动恢复登录 Mixin）
- [x] 5.2 从 `core/monitor.py` 中移动自动恢复相关方法（auto_recover_login、_prune_auto_recover_attempts、_mark_auto_recover_result、_reset_auto_recover_2fa_waiting、_classify_failure_reason、_record_disconnect_reason、_record_auto_recover_success、_record_auto_recover_failure、get_auto_recover_status、_log_friends_api_readiness）到 `MonitorRecoveryMixin` 类
- [x] 5.3 在 `core/monitor_recovery.py` 中添加必要的导入（from core.vrchat_errors import ...，以及其他依赖）
- [x] 5.4 修改 `core/monitor.py` 中的 `MonitorService` 类，使其继承 `MonitorRecoveryMixin`
- [x] 5.5 运行 `python -m py_compile core/monitor_recovery.py` 和 `python -m py_compile core/monitor.py` 验证无语法错误

## Task 6: 提取监控同房检测模块 (monitor_coroom.py) [depends:5]

- [x] 6.1 创建 `core/monitor_coroom.py`，添加模块文档字符串（监控服务同房检测 Mixin）
- [x] 6.2 从 `core/monitor.py` 中移动同房检测方法（_build_coroom_events、_filter_coroom_events_by_interval、list_coroom_groups）到 `MonitorCoroomMixin` 类
- [x] 6.3 在 `core/monitor_coroom.py` 中添加必要的导入
- [x] 6.4 修改 `core/monitor.py` 中的 `MonitorService` 类，使其继承 `MonitorCoroomMixin`
- [x] 6.5 运行 `python -m py_compile core/monitor_coroom.py` 和 `python -m py_compile core/monitor.py` 验证无语法错误
- [x] 6.6 确认 `core/monitor.py` 瘦身后约 930 行，低于 1000 行限制

## Task 7: 提取图片渲染工具模块 (rendering.py) [depends:4]

- [x] 7.1 创建 `core/rendering.py`，添加模块文档字符串（图片渲染工具方法 Mixin）
- [x] 7.2 从 `main.py` 中移动渲染相关方法（_get_card_font、_measure_text、_wrap_text、_draw_wrapped_text、_draw_round_rect、_load_cover_image、_paste_card_cover、_short_world_label）到 `RenderingMixin` 类
- [x] 7.3 在 `core/rendering.py` 中添加必要的导入（PIL/Pillow 相关，以及 TYPE_CHECKING 守卫下的主类引用）
- [x] 7.4 在 `main.py` 中添加 `from core.rendering import RenderingMixin`，并将其加入 `VRCFriendRadarPlugin` 的继承列表
- [x] 7.5 运行 `python -m py_compile core/rendering.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 8: 提取插件辅助方法模块 (plugin_helpers.py) [depends:7]

- [x] 8.1 创建 `core/plugin_helpers.py`，添加模块文档字符串（插件通用辅助方法 Mixin）
- [x] 8.2 从 `main.py` 中移动辅助方法（_build_session_key、_cleanup_search_sessions、_save_search_session、_get_search_session、_get_group_id、_is_private_event、_sanitize_display_name_for_output、_build_online_friend_list_message、_cleanup_temp_world_logo_files、_download_image_to_temp、_download_generic_image_to_temp、_get_world_name、_format_world_display、_get_world_info_with_cache、_translate_non_zh_description、_is_text_mostly_chinese、_get_translation_lock、_escape_html、_format_joinability_overview、_get_today_online_friend_ids、_remember_private_admin_sender、_resolve_admin_notice_targets、_track_background_task）到 `PluginHelpersMixin` 类
- [x] 8.3 在 `core/plugin_helpers.py` 中添加必要的导入（TYPE_CHECKING 守卫下的 VRCFriendRadarPlugin 引用）
- [x] 8.4 在 `main.py` 中添加 `from core.plugin_helpers import PluginHelpersMixin`，并将其加入继承列表
- [x] 8.5 运行 `python -m py_compile core/plugin_helpers.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 9: 提取事件分发模块 (event_dispatch.py) [depends:8]

- [x] 9.1 创建 `core/event_dispatch.py`，添加模块文档字符串（事件分发与消息推送 Mixin）
- [x] 9.2 从 `main.py` 中移动事件分发方法（_handle_monitor_events、_handle_monitor_notice、_push_messages_to_notify_groups、_dispatch_events_to_tag_routed_groups、_dispatch_signature_subscriptions、_format_events_for_push、_push_chain_to_notify_groups、_send_chain_to_groups、_send_chain_to_private_users、_push_login_notice_to_admins、_redact_location_detail）到 `EventDispatchMixin` 类
- [x] 9.3 在 `core/event_dispatch.py` 中添加必要的导入（from core.events import ...，TYPE_CHECKING 守卫）
- [x] 9.4 在 `main.py` 中添加 `from core.event_dispatch import EventDispatchMixin`，并将其加入继承列表
- [x] 9.5 运行 `python -m py_compile core/event_dispatch.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 10: 提取灵魂画像核心逻辑模块 (soul_profile.py) [depends:7]

- [x] 10.1 创建 `core/soul_profile.py`，添加模块文档字符串（灵魂画像核心逻辑与渲染 Mixin）
- [x] 10.2 从 `main.py` 中移动灵魂画像核心方法（_build_soul_profile_summary、_render_soul_profile_card、_generate_soul_profile_ai_texts、_build_timeline_worlds、_build_presence_segments、_estimate_companion_match、_draw_timeline_branch_card、_pick_active_periods、_pick_style_tags、_build_resident_label）到 `SoulProfileMixin` 类
- [x] 10.3 从 `main.py` 中移动相关数据类（SoulProfileSummary、ProfileTargetOption、ProfileTargetResolveResult）到新文件
- [x] 10.4 在 `core/soul_profile.py` 中添加必要的导入（TYPE_CHECKING 守卫，以及 rendering 模块的依赖）
- [x] 10.5 在 `main.py` 中添加 `from core.soul_profile import SoulProfileMixin`，并将其加入继承列表
- [x] 10.6 运行 `python -m py_compile core/soul_profile.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 11: 提取每日定时任务模块 (daily_scheduler.py) [depends:9]

- [x] 11.1 创建 `core/daily_scheduler.py`，添加模块文档字符串（每日定时任务调度 Mixin）
- [x] 11.2 从 `main.py` 中移动定时任务方法（_handle_loop_tick、_daily_task_should_run、_get_daily_task_last_sent_date、_set_daily_task_last_sent_date、_send_daily_report_to_notify_groups）到 `DailySchedulerMixin` 类
- [x] 11.3 在 `core/daily_scheduler.py` 中添加必要的导入（TYPE_CHECKING 守卫）
- [x] 11.4 在 `main.py` 中添加 `from core.daily_scheduler import DailySchedulerMixin`，并将其加入继承列表
- [x] 11.5 运行 `python -m py_compile core/daily_scheduler.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 12: 创建 commands 目录并提取登录命令模块 (login_commands.py) [depends:8]

- [x] 12.1 创建 `commands/` 目录和 `commands/__init__.py` 文件，在 `__init__.py` 中预留所有 Mixin 的导出声明
- [x] 12.2 创建 `commands/login_commands.py`，添加模块文档字符串（登录相关命令 Mixin）
- [x] 12.3 从 `main.py` 中移动登录命令方法（interactive_login、submit_code、clear_login、_parse_login_credentials、_post_login_auto_sync_and_reply）到 `LoginCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 12.4 在 `commands/login_commands.py` 中添加必要的导入（from __future__ import annotations，TYPE_CHECKING 守卫）
- [x] 12.5 更新 `commands/__init__.py` 导出 `LoginCommandsMixin`
- [x] 12.6 在 `main.py` 中添加 `from commands import LoginCommandsMixin`，并将其加入继承列表
- [x] 12.7 运行 `python -m py_compile commands/login_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 13: 提取 B 站解析命令模块 (bili_commands.py) [depends:12]

- [x] 13.1 创建 `commands/bili_commands.py`，添加模块文档字符串（B站视频解析命令 Mixin）
- [x] 13.2 从 `main.py` 中移动 B 站命令方法（bili_parse_command、bili_cover_command、_get_bili_parser、_read_config_value）到 `BiliCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 13.3 在 `commands/bili_commands.py` 中添加必要的导入
- [x] 13.4 更新 `commands/__init__.py` 导出 `BiliCommandsMixin`
- [x] 13.5 在 `main.py` 中添加继承 `BiliCommandsMixin`
- [x] 13.6 运行 `python -m py_compile commands/bili_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 14: 提取通知中心命令模块 (notification_commands.py) [depends:12]

- [x] 14.1 创建 `commands/notification_commands.py`，添加模块文档字符串（通知中心与审批命令 Mixin）
- [x] 14.2 从 `main.py` 中移动通知命令方法（notification_center、approve_notification、accept_invite、reject_invite、_pick_pending_notification、_handle_new_vrc_notifications）到 `NotificationCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 14.3 在 `commands/notification_commands.py` 中添加必要的导入
- [x] 14.4 更新 `commands/__init__.py` 导出 `NotificationCommandsMixin`
- [x] 14.5 在 `main.py` 中添加继承 `NotificationCommandsMixin`
- [x] 14.6 运行 `python -m py_compile commands/notification_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 15: 提取好友管理命令模块 (friend_commands.py) [depends:12]

- [x] 15.1 创建 `commands/friend_commands.py`，添加模块文档字符串（好友管理命令 Mixin）
- [x] 15.2 从 `main.py` 中移动好友命令方法（add_watch_friend、remove_watch_friend、show_watch_list、search_friends、friend_list、online_friend_list、add_watch_by_index、search_worlds、global_search_users、export_friends）到 `FriendCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 15.3 在 `commands/friend_commands.py` 中添加必要的导入
- [x] 15.4 更新 `commands/__init__.py` 导出 `FriendCommandsMixin`
- [x] 15.5 在 `main.py` 中添加继承 `FriendCommandsMixin`
- [x] 15.6 运行 `python -m py_compile commands/friend_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 16: 提取社交互动命令模块 (social_commands.py) [depends:12]

- [x] 16.1 创建 `commands/social_commands.py`，添加模块文档字符串（社交互动命令 Mixin）
- [x] 16.2 从 `main.py` 中移动社交命令方法（boop_friend、invite_to_instance、public_friend_request、toggle_public_friend_request、user_profile、friendship_history、friend_note_set、friend_note_list、instance_info、server_status、coroom_status）到 `SocialCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 16.3 在 `commands/social_commands.py` 中添加必要的导入
- [x] 16.4 更新 `commands/__init__.py` 导出 `SocialCommandsMixin`
- [x] 16.5 在 `main.py` 中添加继承 `SocialCommandsMixin`
- [x] 16.6 运行 `python -m py_compile commands/social_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 17: 提取报告统计命令模块 (report_commands.py) [depends:11,12]

- [x] 17.1 创建 `commands/report_commands.py`，添加模块文档字符串（日报/周报/统计命令 Mixin）
- [x] 17.2 从 `main.py` 中移动报告命令方法（generate_daily_report、weekly_report、hot_worlds、export_events、activity_heatmap、_render_activity_heatmap、_build_daily_report_components、_collect_hot_world_stats_today）到 `ReportCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 17.3 在 `commands/report_commands.py` 中添加必要的导入
- [x] 17.4 更新 `commands/__init__.py` 导出 `ReportCommandsMixin`
- [x] 17.5 在 `main.py` 中添加继承 `ReportCommandsMixin`
- [x] 17.6 运行 `python -m py_compile commands/report_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 18: 提取管理命令模块 (admin_commands.py) [depends:12]

- [x] 18.1 创建 `commands/admin_commands.py`，添加模块文档字符串（管理与标签命令 Mixin）
- [x] 18.2 从 `main.py` 中移动管理命令方法（bind_notify_group、unbind_notify_group、show_notify_groups、tag_bind_group、tag_unbind_group、tag_list、tag_friend、toggle_group_privacy、subscribe_signature_keyword、unsubscribe_signature_keyword、list_signature_subscriptions、toggle_adaptive_polling、status、test_notify、push_test、detect_changes、sync_friends、recent_events、help_menu）到 `AdminCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 18.3 在 `commands/admin_commands.py` 中添加必要的导入
- [x] 18.4 更新 `commands/__init__.py` 导出 `AdminCommandsMixin`
- [x] 18.5 在 `main.py` 中添加继承 `AdminCommandsMixin`
- [x] 18.6 运行 `python -m py_compile commands/admin_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 19: 提取灵魂画像命令模块 (soul_profile_commands.py) [depends:10,12]

- [x] 19.1 创建 `commands/soul_profile_commands.py`，添加模块文档字符串（灵魂画像命令 Mixin）
- [x] 19.2 从 `main.py` 中移动灵魂画像命令方法（weekly_soul_profile、persona_only、fortune_only、relationship_score、_build_public_soul_profile_image、_resolve_profile_target 系列方法、_split_relationship_targets、_resolve_two_profile_targets_interactive）到 `SoulProfileCommandsMixin` 类，保留所有 @filter 装饰器
- [x] 19.3 在 `commands/soul_profile_commands.py` 中添加必要的导入（from core.soul_profile import SoulProfileSummary 等数据类）
- [x] 19.4 更新 `commands/__init__.py` 导出 `SoulProfileCommandsMixin`
- [x] 19.5 在 `main.py` 中添加继承 `SoulProfileCommandsMixin`
- [x] 19.6 运行 `python -m py_compile commands/soul_profile_commands.py` 和 `python -m py_compile main.py` 验证无语法错误

## Task 20: 清理 main.py 并进行全量验证 [depends:13,14,15,16,17,18,19]

- [x] 20.1 清理 `main.py`，移除所有已迁移到 Mixin 的方法，仅保留类定义、__init__、initialize、terminate、_register_llm_tools、_reconcile_dynamic_lists_on_startup、_sync_runtime_config_lists_from_repo
- [x] 20.2 确认 `main.py` 最终行数约 200 行，低于 1000 行限制
- [x] 20.3 确认 `commands/__init__.py` 正确导出所有 8 个命令 Mixin（LoginCommandsMixin、FriendCommandsMixin、SoulProfileCommandsMixin、NotificationCommandsMixin、ReportCommandsMixin、SocialCommandsMixin、AdminCommandsMixin、BiliCommandsMixin）
- [x] 20.4 运行 `python -m py_compile main.py` 验证主文件无语法错误
- [x] 20.5 对所有新创建的文件逐一运行 `python -m py_compile` 确认无语法错误
- [x] 20.6 验证无循环导入：运行 `python -c "from main import VRCFriendRadarPlugin"` 确认插件类可正常加载
- [x] 20.7 确认所有文件均在 1000 行以内：检查 main.py (~200行)、monitor.py (~930行)、vrchat_client.py (~500行) 及所有新模块
- [x] 20.8 确认 @filter 装饰器在所有命令 Mixin 中正确保留，命令注册名称和触发方式不变
- [x] 20.9 检查日志无异常导入警告，确认插件可正常初始化和终止
