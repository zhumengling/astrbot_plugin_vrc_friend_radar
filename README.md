# astrbot_plugin_vrc_friend_radar

基于 AstrBot 的 VRChat 好友监控插件。它会定期拉取好友快照，识别并记录状态变化（上线/下线/状态变更/切图/同房），并可自动推送到指定群。


---


## 核心功能

- VRChat 账号登录（支持 2FA：邮箱验证码 / TOTP / Recovery Code）
- 登录态持久化与自动恢复（`session.json`）
- 好友数据同步与本地缓存（SQLite）
- 变化检测：
  - `friend_online`
  - `friend_offline`
  - `status_changed`
  - `status_message_changed`
  - `location_changed`
  - `co_room`（同房提醒）
- 事件去重（时间窗口内相同事件仅记/推一次）
- 自动推送到通知群，支持手动触发检测
- 本地好友搜索、按序号快速加入监控
- VRChat 世界搜索（含首条结果图片预览）
- 在线好友列表（带世界名解析 + joinability 判断）
- 每日自动日报（可选）与手动生成日报
- 今日热门世界统计（基于“今天上线过的好友”口径）
- 推荐世界简介自动翻译（依赖 AstrBot 当前可用 LLM；失败会回退原文）

---

## 安装方式

### 1）放置插件

将插件目录放到 AstrBot 插件目录（或通过 AstrBot 插件市场/仓库方式安装），目录名应为：

```text
astrbot_plugin_vrc_friend_radar
```

### 2）安装依赖

在插件目录执行：

```bash
pip install -r requirements.txt
```

### 3）重载/重启 AstrBot

确保插件加载成功后，再进行登录与配置。

---

## 依赖说明

来自 `requirements.txt`：

```text
vrchatapi>=1.19.1
httpx>=0.27.0
pyotp>=2.9.0
```

说明：
- 核心 API 调用由 `vrchatapi` 完成；
- 插件还会调用 AstrBot 的消息发送与（可选）LLM 能力（用于世界简介翻译）。

---

## 配置项说明

以下基于 `_conf_schema.json` + `core/config.py` 当前实际行为。

> 注意：部分数值在代码内会被二次约束（例如最小值/最大值），表内已标注。

| 配置项 | 类型 | 默认值 | 实际说明（含约束） |
|---|---|---:|---|
| `poll_interval_seconds` | int | 180 | 轮询间隔秒数；代码最小强制 `60`。 |
| `notify_group_ids` | list[string] | `[]` | 通知群号列表。支持运行中命令维护，并会与数据库持久化列表同步。 |
| `watch_friend_ids` | list[string] | `[]` | 监控好友 ID 列表。为空时不会产出监控事件（但好友缓存/搜索功能仍可使用）。 |
| `watch_self` | bool | false | 是否监控登录账号自己；会把自己加入“有效监控集合”（不写回列表）。 |
| `enable_status_tracking` | bool | true | 是否追踪状态相关事件（上线/下线/状态/签名）。 |
| `enable_world_tracking` | bool | true | 是否追踪 `location_changed`。 |
| `vrchat_user_agent` | string | `AstrBotVRCFriendRadar/0.1.0` | VRChat User-Agent；默认推荐使用插件标识型 UA（非浏览器伪装）。若需联系方式，可在末尾追加邮箱后缀。 |
| `login_session_timeout_seconds` | int | 30 | 登录交互超时；代码约束范围 `30~600`。 |
| `event_dedupe_window_seconds` | int | 300 | 事件去重窗口；代码最小 `30`。 |
| `event_batch_size` | int | 10 | 单次推送最大事件条数；代码约束 `1~50`。 |
| `allow_auto_push` | bool | true | 是否开启轮询自动推送；关闭后可手动 `/vrc检测变化`。 |
| `notify_location_detail` | bool | true | 位置展示细化开关（用于位置文本格式化）。 |
| `search_result_ttl_seconds` | int | 120 | 搜索结果会话有效期（用于“按序号添加监控”）；代码最小 `30`。 |
| `coroom_notify_interval_seconds` | int | 600 | 同房提醒最短推送间隔；代码最小 `30`。 |
| `coroom_notify_min_members` | int | 2 | 判定同房的人数阈值；代码最小 `2`。 |
| `coroom_notify_joinable_only` | bool | false | 自动同房提醒是否只推“可加入”实例。 |
| `enable_daily_report` | bool | false | 是否开启自动日报（每天一次）。 |
| `daily_task_time` | string(HH:MM) | `21:00` | 每日任务默认时间；格式非法会回退默认值。 |
| `daily_report_time` | string(HH:MM) | `21:00` | 日报独立触发时间；若未配置则继承 `daily_task_time`。 |
| `daily_report_top_n` | int | 5 | 日报 TopN/热门世界默认 N；代码约束 `1~20`。 |
| `world_translation_cache_max_entries` | int | 500 | 世界简介翻译缓存最大条目数；`0` 表示不清理。 |

---

## 命令列表（以 `main.py` 为准）

### 管理员命令

- `/vrc状态`：查看运行状态摘要（含 Web/API 登录态 与 当前账号客户端在线态区分）
- `/vrc测试`：插件在线测试
- `/vrc推送测试`：向通知群发测试推送
- `/vrc解绑登录`：清除本地持久化登录态
- `/vrc绑定通知群`：在群聊中把当前群设为通知群
- `/vrc解绑通知群`：在群聊中移除当前通知群
- `/vrc通知群`：查看通知群列表
- `/vrc添加监控 usr_xxx`：按好友 ID 添加监控
- `/vrc删除监控 usr_xxx`：按好友 ID 删除监控
- `/vrc监控列表`：查看监控列表
- `/vrc添加监控序号 N`：将最近一次“搜索好友”结果第 N 项加入监控
- `/vrc登录 用户名 密码`：发起登录（群聊会被拒绝，要求私聊）
- `/vrc验证码 123456`：提交 2FA 验证码（群聊会被拒绝，要求私聊）
- `/vrc同步好友`：同步好友快照到本地缓存
- `/vrc好友列表 [页码]`
- `/vrc在线好友 [页码]`
- `/vrc检测变化`：手动执行一次变化检测
- `/vrc同房情况`：查看当前同房分组
- `/vrc生成日报 [推送]`：生成日报；参数 `推送` 时推送到通知群
- `/vrc最近事件`：查看最近事件

### 非管理员也可用

- `/vrc搜索地图 关键词`
- `/vrc搜索好友 关键词 [页码]`
- `/vrc热门世界 [N]`

---

## 使用流程（推荐）

### 1）首次使用

1. 安装依赖并启动插件
2. 先执行 `/vrc状态`，确认插件已加载

### 2）登录

1. **私聊**机器人发送：`/vrc登录 用户名 密码`
2. 若触发二步验证，按提示在超时前发送：`/vrc验证码 123456`

> 插件会拒绝在群里接收登录信息/验证码，避免泄漏。

### 3）同步好友

登录成功后可执行：`/vrc同步好友`

- 成功后会写入本地缓存；
- 私聊场景下，登录成功后插件还会尝试自动回传在线好友列表。

### 4）设置通知群 / 监控对象

1. 在目标群发送：`/vrc绑定通知群`
2. 添加监控对象（两种方式）：
   - 直接 ID：`/vrc添加监控 usr_xxx`
   - 先搜索再按序号：`/vrc搜索好友 关键词` + `/vrc添加监控序号 N`

### 5）检查效果

- 手动检测：`/vrc检测变化`
- 查看同房：`/vrc同房情况`
- 查看近期事件：`/vrc最近事件`

### 6）日报与热门世界

- 查看热门世界：`/vrc热门世界` 或 `/vrc热门世界 10`
- 手动生成日报：`/vrc生成日报`
- 手动推送日报到通知群：`/vrc生成日报 推送`
- 自动日报：开启 `enable_daily_report=true` 并配置时间

---

## 自我监控 / 在线状态判定说明（重要）

### 1）`watch_self` 的实际行为

- 开启后，插件会把“当前登录账号自己”纳入有效监控集合；
- 若重启后内存中 self id 丢失，插件会主动调用 `fetch_self_snapshot()` 刷新；
- 自己的位置信息在不可见/未知场景下会抑制噪声切图事件（仅在可识别实例之间切换时追踪）。

### 2）API/Web 在线 与 真实客户端在线 的区别

当前代码在 `vrchat_client.py` 里做了显式收敛：

- 若判定为 **web presence**（仅网页/API 活跃而非真实进入世界），会被归一为 `offline`；
- 对自我监控场景，若状态看似在线但没有有效世界位置，也会按离线处理，避免“挂网页=在线”误报；
- 因此插件语义更接近：**“在 VRChat 客户端内可感知在线/在世界中”**，而不是“账号 token 还活着”。

这也是“API/Web 登录在线”与“真实客户端在线”的核心差异。

### 3）Joinability（可加入性）

消息中的“可加入/不可进入/未知”由 location 实例标记推断，不等于绝对可传送成功（仍受权限、关系、组设置等影响）。

---


## 本次稳定性增强（2026-04）

- 增强 `vrc状态` 可观测性：明确区分 **Web/API登录态** 与 **当前账号客户端在线态**（避免“网页在线=客户端在线”误判）。
- 修复自动恢复2FA等待超时后的状态僵死：超时会自动退出等待并恢复后续自动恢复能力。
- 监控循环停止时增加会话强制落盘，降低重启前 cookie 丢失风险。
- 图片下载链路改为复用统一 cookie 提取逻辑，提升 world 图下载成功率。
- 插件生命周期清理补充：停止时释放搜索会话与翻译锁缓存，降低长时间运行内存残留。

## 常见问题 / 注意事项

1. **轮询频率建议**  
   不建议低于 60 秒；默认 180 秒通常更稳妥。

---

### 登录/恢复行为说明

- 登录成功判定以 Auth 成功为准：`get_current_user`（含2FA后）成功即视为登录成功，不再将 friends API 作为登录硬门槛。
- 登录阶段若出现疑似认证污染（401/403 等），会执行一次“清 cookie + 重建 client”的单次重试（类似 VRCX clearCookiesTryLogin 思路）。
- 启动 restore 或自动恢复 restore 失败后，会先清理旧会话状态并重建 client，再进行账号密码重登，避免脏 cookie/脏 client 反复污染。
- 错误提示细分：用户名/密码错误、2FA、认证失效、网络异常分开提示。

