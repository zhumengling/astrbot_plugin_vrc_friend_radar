# 代码重构设计文档

## 概述

本设计文档描述了将 VRC Friend Radar 插件中 3 个超大文件拆分为更小模块的方案。目标是将每个文件控制在 1000 行以内，提升代码可维护性和可读性。

### 当前状态

| 文件 | 行数 | 主要类 |
|------|------|--------|
| `main.py` | 3614 | VRCFriendRadarPlugin |
| `core/monitor.py` | 1303 | MonitorService |
| `core/vrchat_client.py` | 1299 | VRChatClient |

### 设计目标

- 每个模块不超过 1000 行
- 保持对外接口不变（插件入口仍为 `main.py` 中的 `VRCFriendRadarPlugin`）
- 使用 Mixin 模式保持 `self` 访问能力
- 避免循环导入
- 支持渐进式迁移

---

## 架构设计

### 新模块结构总览

```
astrbot_plugin_vrc_friend_radar/
├── main.py                          # ~200 行，插件入口 + 类定义
├── commands/                        # 命令处理模块
│   ├── __init__.py                  # 导出所有 Mixin
│   ├── login_commands.py            # ~150 行 登录相关命令
│   ├── friend_commands.py           # ~300 行 好友管理命令
│   ├── soul_profile_commands.py     # ~200 行 灵魂画像命令
│   ├── notification_commands.py     # ~200 行 通知中心命令
│   ├── report_commands.py           # ~300 行 报告/统计命令
│   ├── social_commands.py           # ~250 行 社交互动命令
│   ├── admin_commands.py            # ~250 行 管理/标签命令
│   └── bili_commands.py             # ~100 行 B站解析命令
├── core/                            # 核心逻辑模块
│   ├── __init__.py
│   ├── rendering.py                 # ~300 行 图片渲染工具
│   ├── soul_profile.py              # ~500 行 灵魂画像核心逻辑
│   ├── event_dispatch.py            # ~250 行 事件分发逻辑
│   ├── plugin_helpers.py            # ~200 行 插件辅助方法
│   ├── daily_scheduler.py           # ~100 行 每日定时任务
│   ├── monitor.py                   # ~930 行 监控服务（瘦身后）
│   ├── monitor_recovery.py          # ~200 行 自动恢复登录
│   ├── monitor_coroom.py            # ~170 行 同房检测
│   ├── vrchat_client.py             # ~500 行 VRChat客户端（瘦身后）
│   ├── vrchat_auth.py               # ~300 行 认证/会话管理
│   ├── vrchat_social.py             # ~250 行 社交API
│   ├── vrchat_world.py              # ~200 行 世界/实例API
│   ├── vrchat_errors.py             # ~50 行  错误类定义
│   └── ... (其他已有模块不变)
└── ...
```

### 依赖关系图

```
┌─────────────────────────────────────────────────────────────┐
│                        main.py                               │
│  VRCFriendRadarPlugin(Star, *AllMixins)                      │
│  __init__, initialize, terminate                             │
└──────────────┬───────────────────────────────────────────────┘
               │ 继承
    ┌──────────┼──────────────────────────────────┐
    │          │                                   │
    ▼          ▼                                   ▼
┌────────┐ ┌────────────┐              ┌──────────────────┐
│commands/│ │  core/     │              │  core/           │
│ *Mixin  │ │ rendering  │              │ event_dispatch   │
│ 命令层  │ │ soul_prof  │              │ plugin_helpers   │
│        │ │ daily_sched│              │ daily_scheduler  │
└───┬────┘ └────────────┘              └──────────────────┘
    │ 调用
    ▼
┌─────────────────────────────────────────────────────────────┐
│                    core/monitor.py                           │
│  MonitorService(MonitorRecoveryMixin, MonitorCoroomMixin)    │
└──────────────┬───────────────────────────────────────────────┘
               │ 调用
               ▼
┌─────────────────────────────────────────────────────────────┐
│                  core/vrchat_client.py                       │
│  VRChatClient(VRChatAuthMixin, VRChatSocialMixin,           │
│               VRChatWorldMixin)                              │
└─────────────────────────────────────────────────────────────┘
```
---

## 模块拆分方案

### 一、main.py 拆分方案（3614 行 → ~200 行 + 多个模块）

#### 拆分策略：Mixin 模式

由于所有命令处理器都需要访问 `self`（插件实例），采用 Mixin 类模式。`VRCFriendRadarPlugin` 通过多重继承获得所有命令能力。

#### 1.1 commands/login_commands.py (~150 行)

**类名**: `LoginCommandsMixin`

**包含方法**:
- `interactive_login` - 交互式登录流程
- `submit_code` - 提交两步验证码
- `clear_login` - 清除登录状态
- `_parse_login_credentials` - 解析登录凭据
- `_post_login_auto_sync_and_reply` - 登录后自动同步并回复

#### 1.2 commands/friend_commands.py (~300 行)

**类名**: `FriendCommandsMixin`

**包含方法**:
- `add_watch_friend` - 添加关注好友
- `remove_watch_friend` - 移除关注好友
- `show_watch_list` - 显示关注列表
- `search_friends` - 搜索好友
- `friend_list` - 好友列表
- `online_friend_list` - 在线好友列表
- `add_watch_by_index` - 按索引添加关注
- `search_worlds` - 搜索世界
- `global_search_users` - 全局搜索用户
- `export_friends` - 导出好友数据

#### 1.3 commands/soul_profile_commands.py (~200 行)

**类名**: `SoulProfileCommandsMixin`

**包含方法**:
- `weekly_soul_profile` - 周灵魂画像
- `persona_only` - 仅人格分析
- `fortune_only` - 仅运势分析
- `relationship_score` - 关系评分
- `_build_public_soul_profile_image` - 构建公开灵魂画像图片
- `_resolve_profile_target*` - 解析画像目标系列方法
- `_split_relationship_targets` - 拆分关系目标
- `_resolve_two_profile_targets_interactive` - 交互式解析双目标

#### 1.4 commands/notification_commands.py (~200 行)

**类名**: `NotificationCommandsMixin`

**包含方法**:
- `notification_center` - 通知中心
- `approve_notification` - 批准通知
- `accept_invite` - 接受邀请
- `reject_invite` - 拒绝邀请
- `_pick_pending_notification` - 选择待处理通知
- `_handle_new_vrc_notifications` - 处理新VRC通知

#### 1.5 commands/report_commands.py (~300 行)

**类名**: `ReportCommandsMixin`

**包含方法**:
- `generate_daily_report` - 生成日报
- `weekly_report` - 周报
- `hot_worlds` - 热门世界
- `export_events` - 导出事件
- `activity_heatmap` - 活跃度热力图
- `_render_activity_heatmap` - 渲染热力图
- `_build_daily_report_components` - 构建日报组件
- `_collect_hot_world_stats_today` - 收集今日热门世界统计

#### 1.6 commands/social_commands.py (~250 行)

**类名**: `SocialCommandsMixin`

**包含方法**:
- `boop_friend` - Boop 好友
- `invite_to_instance` - 邀请到实例
- `public_friend_request` - 公开好友请求
- `toggle_public_friend_request` - 切换公开好友请求
- `user_profile` - 用户资料
- `friendship_history` - 友谊历史
- `friend_note_set` - 设置好友备注
- `friend_note_list` - 好友备注列表
- `instance_info` - 实例信息
- `server_status` - 服务器状态
- `coroom_status` - 同房状态

#### 1.7 commands/admin_commands.py (~250 行)

**类名**: `AdminCommandsMixin`

**包含方法**:
- `bind_notify_group` / `unbind_notify_group` - 绑定/解绑通知群
- `show_notify_groups` - 显示通知群列表
- `tag_bind_group` / `tag_unbind_group` - 标签绑定/解绑群
- `tag_list` / `tag_friend` - 标签列表/标记好友
- `toggle_group_privacy` - 切换群隐私
- `subscribe_signature_keyword` / `unsubscribe_signature_keyword` - 签名关键词订阅
- `list_signature_subscriptions` - 列出签名订阅
- `toggle_adaptive_polling` - 切换自适应轮询
- `status` / `test_notify` / `push_test` - 状态/测试通知
- `detect_changes` / `sync_friends` - 检测变更/同步好友
- `recent_events` / `help_menu` - 最近事件/帮助菜单

#### 1.8 commands/bili_commands.py (~100 行)

**类名**: `BiliCommandsMixin`

**包含方法**:
- `bili_parse_command` - B站视频解析
- `bili_cover_command` - B站封面提取
- `_get_bili_parser` - 获取B站解析器实例
- `_read_config_value` - 读取配置值

#### 1.9 commands/__init__.py

```python
from .login_commands import LoginCommandsMixin
from .friend_commands import FriendCommandsMixin
from .soul_profile_commands import SoulProfileCommandsMixin
from .notification_commands import NotificationCommandsMixin
from .report_commands import ReportCommandsMixin
from .social_commands import SocialCommandsMixin
from .admin_commands import AdminCommandsMixin
from .bili_commands import BiliCommandsMixin

__all__ = [
    "LoginCommandsMixin",
    "FriendCommandsMixin",
    "SoulProfileCommandsMixin",
    "NotificationCommandsMixin",
    "ReportCommandsMixin",
    "SocialCommandsMixin",
    "AdminCommandsMixin",
    "BiliCommandsMixin",
]
```

#### 1.10 core/rendering.py (~300 行)

**类名**: `RenderingMixin`（或独立函数模块）

**包含方法**:
- `_get_card_font` - 获取卡片字体
- `_measure_text` - 测量文本宽度
- `_wrap_text` - 文本换行
- `_draw_wrapped_text` - 绘制换行文本
- `_draw_round_rect` - 绘制圆角矩形
- `_load_cover_image` - 加载封面图片
- `_paste_card_cover` - 粘贴卡片封面
- `_short_world_label` - 短世界标签

**设计说明**: 渲染方法大多为纯函数（仅依赖参数），可考虑设计为独立函数而非 Mixin。但为保持迁移简便性，初期仍使用 Mixin 模式，后续可逐步改为独立函数。

#### 1.11 core/soul_profile.py (~500 行)

**类名**: `SoulProfileMixin`

**包含方法**:
- `_build_soul_profile_summary` - 构建灵魂画像摘要
- `_render_soul_profile_card` - 渲染灵魂画像卡片
- `_generate_soul_profile_ai_texts` - AI生成灵魂画像文本
- `_build_timeline_worlds` - 构建时间线世界
- `_build_presence_segments` - 构建在线状态段
- `_estimate_companion_match` - 估算同伴匹配度
- `_draw_timeline_branch_card` - 绘制时间线分支卡片
- `_pick_active_periods` - 选取活跃时段
- `_pick_style_tags` - 选取风格标签
- `_build_resident_label` - 构建常驻标签

**数据类**:
- `SoulProfileSummary`
- `ProfileTargetOption`
- `ProfileTargetResolveResult`

#### 1.12 core/event_dispatch.py (~250 行)

**类名**: `EventDispatchMixin`

**包含方法**:
- `_handle_monitor_events` - 处理监控事件
- `_handle_monitor_notice` - 处理监控通知
- `_push_messages_to_notify_groups` - 推送消息到通知群
- `_dispatch_events_to_tag_routed_groups` - 按标签路由分发事件
- `_dispatch_signature_subscriptions` - 分发签名订阅
- `_format_events_for_push` - 格式化事件用于推送
- `_push_chain_to_notify_groups` - 推送消息链到通知群
- `_send_chain_to_groups` - 发送消息链到群组
- `_send_chain_to_private_users` - 发送消息链到私聊用户
- `_push_login_notice_to_admins` - 推送登录通知给管理员
- `_redact_location_detail` - 脱敏位置详情

#### 1.13 core/plugin_helpers.py (~200 行)

**类名**: `PluginHelpersMixin`

**包含方法**:
- `_build_session_key` - 构建会话键
- `_cleanup_search_sessions` / `_save_search_session` / `_get_search_session` - 搜索会话管理
- `_get_group_id` - 获取群组ID
- `_is_private_event` - 判断是否私聊事件
- `_sanitize_display_name_for_output` - 清理显示名称
- `_build_online_friend_list_message` - 构建在线好友列表消息
- `_cleanup_temp_world_logo_files` - 清理临时世界Logo文件
- `_download_image_to_temp` / `_download_generic_image_to_temp` - 下载图片到临时目录
- `_get_world_name` / `_format_world_display` - 世界名称/显示格式化
- `_get_world_info_with_cache` - 带缓存获取世界信息
- `_translate_non_zh_description` / `_is_text_mostly_chinese` - 翻译/中文检测
- `_get_translation_lock` - 获取翻译锁
- `_escape_html` - HTML转义
- `_format_joinability_overview` - 格式化可加入性概览
- `_get_today_online_friend_ids` - 获取今日在线好友ID
- `_remember_private_admin_sender` / `_resolve_admin_notice_targets` - 管理员通知目标
- `_track_background_task` - 追踪后台任务

#### 1.14 core/daily_scheduler.py (~100 行)

**类名**: `DailySchedulerMixin`

**包含方法**:
- `_handle_loop_tick` - 处理循环tick
- `_daily_task_should_run` - 判断每日任务是否应执行
- `_get_daily_task_last_sent_date` - 获取每日任务上次发送日期
- `_set_daily_task_last_sent_date` - 设置每日任务上次发送日期
- `_send_daily_report_to_notify_groups` - 发送日报到通知群

#### 拆分后 main.py 保留内容 (~200 行)

```python
from astrbot.api.star import Star
from commands import (
    LoginCommandsMixin, FriendCommandsMixin,
    SoulProfileCommandsMixin, NotificationCommandsMixin,
    ReportCommandsMixin, SocialCommandsMixin,
    AdminCommandsMixin, BiliCommandsMixin,
)
from core.rendering import RenderingMixin
from core.soul_profile import SoulProfileMixin
from core.event_dispatch import EventDispatchMixin
from core.plugin_helpers import PluginHelpersMixin
from core.daily_scheduler import DailySchedulerMixin


class VRCFriendRadarPlugin(
    Star,
    LoginCommandsMixin,
    FriendCommandsMixin,
    SoulProfileCommandsMixin,
    NotificationCommandsMixin,
    ReportCommandsMixin,
    SocialCommandsMixin,
    AdminCommandsMixin,
    BiliCommandsMixin,
    RenderingMixin,
    SoulProfileMixin,
    EventDispatchMixin,
    PluginHelpersMixin,
    DailySchedulerMixin,
):
    """VRChat 好友雷达插件主类"""

    def __init__(self, context):
        super().__init__(context)
        # 初始化代码...

    async def initialize(self):
        # 插件初始化逻辑...

    async def terminate(self):
        # 插件终止逻辑...

    def _register_llm_tools(self):
        # 注册LLM工具...

    async def _reconcile_dynamic_lists_on_startup(self):
        # 启动时协调动态列表...

    async def _sync_runtime_config_lists_from_repo(self):
        # 从仓库同步运行时配置列表...
```
---

### 二、monitor.py 拆分方案（1303 行 → ~930 行 + 2 个模块）

#### 拆分策略：Mixin 模式

从 `MonitorService` 中提取两个独立的功能模块作为 Mixin，使主文件降至 1000 行以下。

#### 2.1 core/monitor_recovery.py (~200 行)

**类名**: `MonitorRecoveryMixin`

**包含方法**:
- `auto_recover_login` - 自动恢复登录主逻辑
- `_prune_auto_recover_attempts` - 清理过期的恢复尝试记录
- `_mark_auto_recover_result` - 标记恢复结果
- `_reset_auto_recover_2fa_waiting` - 重置2FA等待状态
- `_classify_failure_reason` - 分类失败原因
- `_record_disconnect_reason` - 记录断连原因
- `_record_auto_recover_success` - 记录自动恢复成功
- `_record_auto_recover_failure` - 记录自动恢复失败
- `get_auto_recover_status` - 获取自动恢复状态
- `_log_friends_api_readiness` - 记录好友API就绪状态

**设计说明**: 自动恢复逻辑是一个完整的子系统，包含状态追踪、重试策略和错误分类，适合独立为一个模块。

#### 2.2 core/monitor_coroom.py (~170 行)

**类名**: `MonitorCoroomMixin`

**包含方法**:
- `_build_coroom_events` - 构建同房事件
- `_filter_coroom_events_by_interval` - 按间隔过滤同房事件
- `list_coroom_groups` - 列出同房分组

**设计说明**: 同房检测是独立的功能域，有自己的数据结构和过滤逻辑，与核心监控循环解耦。

#### 拆分后 monitor.py 保留内容 (~930 行)

```python
from core.monitor_recovery import MonitorRecoveryMixin
from core.monitor_coroom import MonitorCoroomMixin


class MonitorService(MonitorRecoveryMixin, MonitorCoroomMixin):
    """VRChat 好友状态监控服务"""

    # 保留内容:
    # - 核心监控循环 (_run_loop, start, stop)
    # - 好友同步与变更检测 (sync_friends, detect_changes)
    # - 会话管理 (try_restore_session, persist_session, clear_persisted_session)
    # - 待处理登录管理 (create_pending_login, get_pending_login, pop_pending_login)
    # - 标签/群组路由 (get_friend_tags, set_friend_tags, resolve_event_target_groups)
    # - 自身快照管理 (_safe_fetch_self_snapshot, _record_self_snapshot_failure)
    # - 配置辅助 (get_effective_notify_groups, get_effective_watch_friends, _resolve_poll_interval_seconds)
```

---

### 三、vrchat_client.py 拆分方案（1299 行 → ~500 行 + 4 个模块）

#### 拆分策略：Mixin 模式 + 独立错误模块

将 VRChat API 客户端按功能域拆分为认证、社交、世界三个 Mixin，并将错误类独立为单独模块。

#### 3.1 core/vrchat_errors.py (~50 行)

**独立模块**（非 Mixin）

**包含类**:
- `VRChatClientError` - 基础错误类
- `VRChatTwoFactorRequiredError` - 需要两步验证
- `VRChatAuthInvalidError` - 认证无效
- `VRChatNetworkError` - 网络错误
- `VRChatRateLimitedError` - 速率限制

**设计说明**: 错误类被多个模块引用，独立出来可避免循环导入。

#### 3.2 core/vrchat_auth.py (~300 行)

**类名**: `VRChatAuthMixin`

**包含方法**:
- `_login_sync` - 同步登录
- `restore_session` / `_restore_session_sync` - 恢复会话
- `export_session` - 导出会话
- `_extract_cookie_header` - 提取Cookie头
- `probe_auth_token` / `_probe_auth_token_sync` - 探测认证令牌
- `probe_session_health` / `_probe_session_health_sync` - 探测会话健康
- `verify_session_ready` / `_verify_session_ready_sync` - 验证会话就绪
- `is_logged_in` - 是否已登录
- `get_saved_credentials` - 获取保存的凭据
- `close` - 关闭客户端
- `_create_api_client` - 创建API客户端
- `_request_timeout_tuple` - 请求超时元组

#### 3.3 core/vrchat_social.py (~250 行)

**类名**: `VRChatSocialMixin`

**包含方法**:
- `send_friend_request` - 发送好友请求
- `respond_friend_request` - 响应好友请求
- `boop_user` - Boop用户
- `invite_user_to_instance` - 邀请用户到实例
- `search_users` - 搜索用户
- `get_user_detail` - 获取用户详情
- `update_user_note` - 更新用户备注
- `list_user_groups` - 列出用户群组
- `list_blocked_user_ids` - 列出被屏蔽用户ID
- 以上方法对应的 `_*_sync` 同步版本

#### 3.4 core/vrchat_world.py (~200 行)

**类名**: `VRChatWorldMixin`

**包含方法**:
- `get_world_info` - 获取世界信息
- `search_worlds` - 搜索世界
- `get_instance` - 获取实例
- `list_favorite_worlds` - 列出收藏世界
- `get_server_status` - 获取服务器状态
- 以上方法对应的 `_*_sync` 同步版本

#### 拆分后 vrchat_client.py 保留内容 (~500 行)

```python
from core.vrchat_errors import (
    VRChatClientError, VRChatTwoFactorRequiredError,
    VRChatAuthInvalidError, VRChatNetworkError,
    VRChatRateLimitedError,
)
from core.vrchat_auth import VRChatAuthMixin
from core.vrchat_social import VRChatSocialMixin
from core.vrchat_world import VRChatWorldMixin


class VRChatClient(VRChatAuthMixin, VRChatSocialMixin, VRChatWorldMixin):
    """VRChat API 客户端"""

    # 保留内容:
    # - __init__ 初始化
    # - fetch_friend_snapshots / _fetch_friend_snapshots_sync - 获取好友快照
    # - fetch_self_snapshot - 获取自身快照
    # - list_notifications / mark_notification_seen - 通知管理
    # - download_image_authenticated - 认证图片下载
    # - _normalize_presence - 标准化在线状态
    # - _build_snapshot_from_user - 从用户数据构建快照
    # - _extract_platform_info - 提取平台信息
    # - _raise_as_client_error - 转换为客户端错误
    # - 其他内部辅助方法
```
---

## 设计模式

### Mixin 模式详解

#### 什么是 Mixin

Mixin 是一种通过多重继承为类添加功能的设计模式。每个 Mixin 类提供一组相关方法，最终由主类通过继承组合在一起。

#### 为什么选择 Mixin

1. **保持 `self` 访问**: 所有命令处理器需要访问插件实例的属性（如 `self.monitor`、`self.client`、`self.repo`），Mixin 天然支持这一点
2. **零运行时开销**: 不需要委托对象或代理，方法直接绑定到实例
3. **渐进式迁移**: 可以逐个 Mixin 迁移，每次迁移后运行测试验证
4. **IDE 友好**: 类型检查和自动补全正常工作
5. **对外接口不变**: `VRCFriendRadarPlugin` 仍然拥有所有方法，外部调用者无需修改

#### Mixin 编写规范

```python
# commands/login_commands.py
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main import VRCFriendRadarPlugin


class LoginCommandsMixin:
    """登录相关命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    # 使用 TYPE_CHECKING 提供类型提示，避免运行时循环导入
    # self 的实际类型在运行时由 VRCFriendRadarPlugin 决定

    async def interactive_login(self: 'VRCFriendRadarPlugin', event, ...):
        """交互式登录流程"""
        # 可以正常访问 self.monitor, self.client 等
        ...

    async def submit_code(self: 'VRCFriendRadarPlugin', event, ...):
        """提交两步验证码"""
        ...
```

#### MRO（方法解析顺序）注意事项

Python 使用 C3 线性化算法确定方法解析顺序。为避免冲突：

1. **Mixin 类不应定义 `__init__`**：初始化逻辑全部保留在主类中
2. **避免方法名冲突**：各 Mixin 的方法应有明确的命名前缀或属于不同功能域
3. **Mixin 放在 Star 之后**：确保 `Star.__init__` 正确调用

```python
class VRCFriendRadarPlugin(
    Star,                    # 框架基类，必须在第一位
    LoginCommandsMixin,      # Mixin 按功能分组排列
    FriendCommandsMixin,
    ...                      # 其他 Mixin
):
    pass
```

---

## 导入策略

### 避免循环导入的核心原则

1. **单向依赖**: Mixin 模块 → 核心模块 → 基础模块（不可反向）
2. **TYPE_CHECKING 守卫**: 仅在类型检查时导入主类，运行时不导入
3. **错误类独立**: `vrchat_errors.py` 不依赖任何其他项目模块
4. **延迟导入**: 必要时在函数体内导入以打破循环

### 导入层次结构

```
层级 0 (基础层): vrchat_errors.py, models.py, config.py, events.py
    ↑
层级 1 (服务层): vrchat_auth.py, vrchat_social.py, vrchat_world.py
    ↑
层级 2 (客户端层): vrchat_client.py
    ↑
层级 3 (监控层): monitor_recovery.py, monitor_coroom.py, monitor.py
    ↑
层级 4 (核心逻辑层): rendering.py, soul_profile.py, event_dispatch.py,
                     plugin_helpers.py, daily_scheduler.py
    ↑
层级 5 (命令层): commands/*.py
    ↑
层级 6 (入口层): main.py
```

### 具体导入示例

```python
# core/vrchat_social.py - 层级1，可导入层级0
from core.vrchat_errors import VRChatClientError, VRChatNetworkError
from core.models import FriendSnapshot


class VRChatSocialMixin:
    # self 的属性（如 self._session）由 VRChatClient.__init__ 初始化
    # 无需导入 VRChatClient 本身
    ...
```

```python
# commands/friend_commands.py - 层级5，可导入层级0-4
from __future__ import annotations
from typing import TYPE_CHECKING
from core.models import FriendSnapshot
from core.events import FriendEvent

if TYPE_CHECKING:
    from main import VRCFriendRadarPlugin


class FriendCommandsMixin:
    async def search_friends(self: 'VRCFriendRadarPlugin', ...):
        results = await self.monitor.client.search_users(...)
        ...
```

### 向后兼容导入

为确保外部代码（如果有）不受影响，在 `core/__init__.py` 中保持原有导出：

```python
# core/__init__.py - 保持向后兼容
from core.vrchat_errors import (
    VRChatClientError,
    VRChatTwoFactorRequiredError,
    VRChatAuthInvalidError,
    VRChatNetworkError,
    VRChatRateLimitedError,
)
from core.vrchat_client import VRChatClient
from core.monitor import MonitorService
```
---

## 文件结构

### 重构后完整目录树

```
astrbot_plugin_vrc_friend_radar/
├── main.py                              # ~200 行 插件入口
├── metadata.yaml
├── requirements.txt
├── commands/                            # 新增：命令模块目录
│   ├── __init__.py                      # 导出所有 CommandMixin
│   ├── login_commands.py                # ~150 行
│   ├── friend_commands.py               # ~300 行
│   ├── soul_profile_commands.py         # ~200 行
│   ├── notification_commands.py         # ~200 行
│   ├── report_commands.py               # ~300 行
│   ├── social_commands.py               # ~250 行
│   ├── admin_commands.py                # ~250 行
│   └── bili_commands.py                  # ~100 行
├── core/
│   ├── __init__.py                      # 向后兼容导出
│   ├── bilibili_parser.py               # 已有，不变
│   ├── config.py                        # 已有，不变
│   ├── db.py                            # 已有，不变
│   ├── diff.py                          # 已有，不变
│   ├── events.py                        # 已有，不变
│   ├── models.py                        # 已有，不变
│   ├── notifications.py                 # 已有，不变
│   ├── notifier.py                      # 已有，不变
│   ├── notifier_aggregator.py           # 已有，不变
│   ├── repository.py                    # 已有，不变
│   ├── search_state.py                  # 已有，不变
│   ├── session_store.py                 # 已有，不变
│   ├── utils.py                         # 已有，不变
│   ├── world_cache.py                   # 已有，不变
│   ├── monitor.py                       # ~930 行（瘦身后）
│   ├── monitor_recovery.py              # ~200 行 新增
│   ├── monitor_coroom.py                # ~170 行 新增
│   ├── vrchat_client.py                 # ~500 行（瘦身后）
│   ├── vrchat_errors.py                 # ~50 行  新增
│   ├── vrchat_auth.py                   # ~300 行 新增
│   ├── vrchat_social.py                 # ~250 行 新增
│   ├── vrchat_world.py                  # ~200 行 新增
│   ├── rendering.py                     # ~300 行 新增
│   ├── soul_profile.py                  # ~500 行 新增
│   ├── event_dispatch.py                # ~250 行 新增
│   ├── plugin_helpers.py                # ~200 行 新增
│   └── daily_scheduler.py               # ~100 行 新增
├── tools/                               # 已有，不变
│   ├── __init__.py
│   ├── runtime.py
│   └── vrc_tools.py
├── assets/                              # 已有，不变
│   └── fonts/
└── skills/                              # 已有，不变
    └── SKILL.md
```

### 行数统计对比

| 维度 | 重构前 | 重构后 |
|------|--------|--------|
| 最大文件行数 | 3614 行 (main.py) | ~930 行 (monitor.py) |
| 超过 1000 行的文件数 | 3 | 0 |
| 总模块数 | 3 个大文件 | 20+ 个小模块 |
| 新增文件数 | - | 17 个 |

---

## 迁移策略

### 迁移原则

1. **逐步迁移**: 每次只迁移一个 Mixin，确保每步都可测试
2. **先底层后上层**: 从依赖最少的模块开始
3. **保持可运行**: 每次迁移后插件应能正常启动和运行
4. **Git 友好**: 每个 Mixin 迁移作为独立 commit

### 迁移顺序

#### 阶段一：基础层拆分（无依赖风险）

1. **Step 1**: 提取 `core/vrchat_errors.py`
   - 风险最低，错误类无外部依赖
   - 更新 `vrchat_client.py` 的导入
   - 更新 `core/__init__.py` 保持向后兼容

2. **Step 2**: 提取 `core/vrchat_auth.py`
   - 将认证方法移入 `VRChatAuthMixin`
   - `VRChatClient` 继承 `VRChatAuthMixin`
   - 验证登录流程正常

3. **Step 3**: 提取 `core/vrchat_social.py`
   - 将社交API方法移入 `VRChatSocialMixin`
   - 验证好友请求、Boop等功能正常

4. **Step 4**: 提取 `core/vrchat_world.py`
   - 将世界/实例API方法移入 `VRChatWorldMixin`
   - 验证世界搜索、实例查询正常

#### 阶段二：监控层拆分

5. **Step 5**: 提取 `core/monitor_recovery.py`
   - 将自动恢复逻辑移入 `MonitorRecoveryMixin`
   - 验证断线重连功能正常

6. **Step 6**: 提取 `core/monitor_coroom.py`
   - 将同房检测逻辑移入 `MonitorCoroomMixin`
   - 验证同房事件生成正常

#### 阶段三：插件核心逻辑拆分

7. **Step 7**: 提取 `core/rendering.py`
   - 图片渲染方法，依赖较少
   - 验证卡片生成正常

8. **Step 8**: 提取 `core/plugin_helpers.py`
   - 辅助方法，被多个命令模块使用
   - 验证各辅助功能正常

9. **Step 9**: 提取 `core/event_dispatch.py`
   - 事件分发逻辑
   - 验证事件推送正常

10. **Step 10**: 提取 `core/soul_profile.py`
    - 灵魂画像核心逻辑（最大的单个模块）
    - 验证灵魂画像生成正常

11. **Step 11**: 提取 `core/daily_scheduler.py`
    - 每日定时任务
    - 验证定时报告正常

#### 阶段四：命令层拆分

12. **Step 12**: 创建 `commands/` 目录和 `__init__.py`

13. **Step 13**: 提取 `commands/login_commands.py`
    - 最简单的命令组，方法最少

14. **Step 14**: 提取 `commands/bili_commands.py`
    - 独立性强，与其他命令无交叉

15. **Step 15**: 提取 `commands/notification_commands.py`

16. **Step 16**: 提取 `commands/friend_commands.py`

17. **Step 17**: 提取 `commands/social_commands.py`

18. **Step 18**: 提取 `commands/report_commands.py`

19. **Step 19**: 提取 `commands/admin_commands.py`

20. **Step 20**: 提取 `commands/soul_profile_commands.py`
    - 依赖 `core/soul_profile.py`，放在最后

#### 阶段五：清理与验证

21. **Step 21**: 清理 `main.py`
    - 移除已迁移的方法
    - 确认只保留类定义和生命周期方法
    - 验证行数 ~200 行

22. **Step 22**: 全量功能验证
    - 运行所有命令确认功能正常
    - 检查日志无异常导入警告
    - 确认所有文件均在 1000 行以内

### 每步迁移的验证清单

每完成一个 Step，执行以下验证：

- [ ] Python 语法检查通过 (`python -m py_compile <file>`)  
- [ ] 插件可正常加载（无 ImportError）
- [ ] 相关功能手动测试通过
- [ ] 无循环导入警告
- [ ] Git commit 记录变更

### 回滚策略

由于采用渐进式迁移，每个 Step 都是独立的 Git commit。如果某步出现问题：

1. `git revert <commit>` 回滚该步
2. 分析问题原因
3. 修复后重新迁移

---

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 循环导入 | 插件无法加载 | 严格遵循导入层次；使用 TYPE_CHECKING 守卫 |
| 装饰器丢失 | 命令不注册 | 迁移时保留所有装饰器；迁移后验证命令列表 |
| MRO 冲突 | 类定义报错 | Mixin 不定义 `__init__`；避免同名方法 |
| 属性访问失败 | 运行时 AttributeError | Mixin 中使用 `self: 'PluginType'` 类型注解辅助开发 |
| 性能退化 | 方法查找变慢 | Python MRO 缓存机制保证性能；实测无可感知差异 |

---

## 总结

本次重构通过 Mixin 模式将 3 个超大文件拆分为 20+ 个职责单一的小模块：

- **main.py**: 3614 行 → ~200 行（命令分散到 8 个 Mixin + 核心逻辑分散到 5 个 Mixin）
- **monitor.py**: 1303 行 → ~930 行（提取 2 个 Mixin）
- **vrchat_client.py**: 1299 行 → ~500 行（提取 3 个 Mixin + 1 个错误模块）

重构后所有文件均在 1000 行以内，代码组织更清晰，便于团队协作和后续维护。