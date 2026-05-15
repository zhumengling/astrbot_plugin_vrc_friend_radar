# 需求文档

## 简介

对 astrbot_plugin_vrc_friend_radar 插件项目进行代码重构，将超过 1000 行的大文件拆分为更小、更具内聚性的模块，同时提升代码可读性和可维护性。重构过程中必须保持所有现有功能不变，不引入行为变更。

## 术语表

- **Plugin**: 基于 AstrBot 框架的 VRChat 好友雷达插件（VRCFriendRadarPlugin 类）
- **Command_Handler**: Plugin 中处理用户命令的异步方法（如 `status`、`interactive_login` 等）
- **MonitorService**: 负责好友状态轮询、变化检测和事件生成的核心服务类
- **VRChatClient**: 封装 VRChat API 调用的客户端类
- **Module**: 一个独立的 Python 文件（.py），包含逻辑上相关的类或函数
- **Refactoring_System**: 执行本次重构的整体系统

## 需求

### 需求 1：文件行数限制

**用户故事：** 作为开发者，我希望每个源文件不超过 1000 行，以便于阅读、导航和维护。

#### 验收标准

1. WHEN Refactoring_System 完成重构后，THE Refactoring_System SHALL 确保项目中每个 Python 源文件不超过 1000 行（含空行和注释）
2. WHEN 原始文件超过 1000 行时，THE Refactoring_System SHALL 将其按逻辑职责拆分为多个独立 Module
3. WHEN 拆分 Module 时，THE Refactoring_System SHALL 确保每个新 Module 具有单一、明确的职责

### 需求 2：main.py 命令处理器拆分

**用户故事：** 作为开发者，我希望 main.py 中的命令处理器按功能域分组到独立模块中，以便快速定位和修改特定命令。

#### 验收标准

1. WHEN 拆分 main.py 时，THE Refactoring_System SHALL 将登录相关命令（login、submit_code、clear_login）提取到独立的登录命令模块
2. WHEN 拆分 main.py 时，THE Refactoring_System SHALL 将好友管理命令（add_watch_friend、remove_watch_friend、show_watch_list、search_friends、friend_list 等）提取到独立的好友命令模块
3. WHEN 拆分 main.py 时，THE Refactoring_System SHALL 将灵魂画像相关功能（soul_profile、persona、fortune、relationship_score）提取到独立的画像模块
4. WHEN 拆分 main.py 时，THE Refactoring_System SHALL 将通知与审批命令（notification_center、approve_notification、accept_invite、reject_invite）提取到独立的通知命令模块
5. WHEN 拆分 main.py 时，THE Refactoring_System SHALL 将日报/周报/统计命令（daily_report、weekly_report、hot_worlds、export_events）提取到独立的报告命令模块
6. WHEN 拆分 main.py 时，THE Refactoring_System SHALL 将图片渲染辅助方法（_get_card_font、_measure_text、_wrap_text、_draw_round_rect 等）提取到独立的渲染工具模块
7. WHEN 拆分 main.py 时，THE Refactoring_System SHALL 保留 VRCFriendRadarPlugin 类作为插件入口，仅包含初始化、生命周期管理和命令注册逻辑

### 需求 3：monitor.py 拆分

**用户故事：** 作为开发者，我希望 MonitorService 中的不同职责被分离到独立模块，以降低单个文件的复杂度。

#### 验收标准

1. WHEN 拆分 monitor.py 时，THE Refactoring_System SHALL 将自动恢复登录逻辑（auto_recover_login 及相关辅助方法）提取到独立模块
2. WHEN 拆分 monitor.py 时，THE Refactoring_System SHALL 将同房检测逻辑（_build_coroom_events、_filter_coroom_events_by_interval）提取到独立模块
3. WHEN 拆分 monitor.py 时，THE Refactoring_System SHALL 确保 MonitorService 主类保持在 1000 行以内

### 需求 4：vrchat_client.py 拆分

**用户故事：** 作为开发者，我希望 VRChatClient 中的 API 调用按功能域分组，以便于理解和扩展。

#### 验收标准

1. WHEN 拆分 vrchat_client.py 时，THE Refactoring_System SHALL 将认证相关逻辑（login、restore_session、export_session、probe_auth_token）提取到独立的认证模块
2. WHEN 拆分 vrchat_client.py 时，THE Refactoring_System SHALL 将社交 API 调用（send_friend_request、respond_friend_request、boop_user、invite_user_to_instance）提取到独立的社交模块
3. WHEN 拆分 vrchat_client.py 时，THE Refactoring_System SHALL 将世界/实例查询 API（get_world_info、search_worlds、get_instance、list_favorite_worlds）提取到独立的世界模块
4. WHEN 拆分 vrchat_client.py 时，THE Refactoring_System SHALL 确保 VRChatClient 主类保持在 1000 行以内

### 需求 5：功能行为保持不变

**用户故事：** 作为用户，我希望重构后插件的所有功能与重构前完全一致，不会出现功能缺失或行为变化。

#### 验收标准

1. THE Refactoring_System SHALL 保持所有公开 API 接口（类名、方法签名、参数）不变
2. THE Refactoring_System SHALL 保持所有命令的注册名称和触发方式不变
3. WHEN 将方法提取到新模块时，THE Refactoring_System SHALL 通过导入确保原有调用路径仍然可用
4. THE Refactoring_System SHALL 保持 metadata.yaml 中声明的所有功能正常工作
5. IF 重构导致循环导入，THEN THE Refactoring_System SHALL 通过调整模块边界或延迟导入解决循环依赖

### 需求 6：代码质量提升

**用户故事：** 作为开发者，我希望重构后的代码更加优雅、可读，减少重复代码。

#### 验收标准

1. WHEN 提取新模块时，THE Refactoring_System SHALL 为每个模块添加模块级文档字符串说明其职责
2. WHEN 发现重复代码模式时，THE Refactoring_System SHALL 将其提取为共享工具函数
3. THE Refactoring_System SHALL 确保所有新模块的命名清晰反映其功能（使用小写下划线命名法）
4. WHEN 拆分类时，THE Refactoring_System SHALL 使用 Mixin 模式或组合模式保持代码组织清晰

### 需求 7：框架兼容性

**用户故事：** 作为开发者，我希望重构后的插件仍然完全兼容 AstrBot 框架的插件加载机制。

#### 验收标准

1. THE Refactoring_System SHALL 保持 main.py 中 VRCFriendRadarPlugin 类继承自 Star 基类
2. THE Refactoring_System SHALL 保持 main.py 作为插件入口文件的角色不变
3. WHEN 命令处理器被移动到其他模块时，THE Refactoring_System SHALL 确保 @filter 装饰器仍然正确注册在 Plugin 实例的方法上
4. THE Refactoring_System SHALL 保持 initialize() 和 terminate() 生命周期方法在 VRCFriendRadarPlugin 类中
