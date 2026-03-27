# astrbot_plugin_vrc_friend_radar

运行于 AstrBot 的 VRChat 好友雷达插件，用于监控好友上线、下线、状态变化与地图切换，并将变化整理后推送到指定群聊。

> 适合在群内持续关注固定好友动态。

---

## 功能特性

- 支持 VRChat 账号登录与二步验证码验证
- 支持登录态持久化恢复
- 支持查看缓存好友列表与在线好友列表
- 支持本地好友搜索与按序号添加监控
- 支持地图搜索，并可附带地图图片预览
- 支持状态变化检测：上线、下线、状态变化、状态签名变化
- 支持地图切换检测与地图名解析缓存
- 支持自动推送与手动检测
- 支持通知群配置
- 支持监控好友白名单

---

### 依赖

```text
vrchatapi>=1.19.1
httpx>=0.27.0
pyotp>=2.9.0
```

### 插件目录名

```text
astrbot_plugin_vrc_friend_radar
```

### 建议安装步骤

```bash
pip install -r requirements.txt
```

---

## 配置说明

插件支持通过配置文件或 AstrBot WebUI 配置以下内容，配置定义见 [`_conf_schema.json`](_conf_schema.json)。

| 配置项 | 说明 | 默认值 |
|---|---|---:|
| `poll_interval_seconds` | 轮询间隔秒数。VRChat 官方建议不要高于每 60 秒一次请求，当前建议设置为 180-300 秒。 | `180` |
| `notify_group_ids` | 通知群号列表。变化播报会推送到这些群。 | `[]` |
| `watch_friend_ids` | 监控好友 ID 列表。为空时可先同步全部好友，再通过命令逐步添加。 | `[]` |
| `enable_status_tracking` | 是否追踪状态变化。关闭后不会产生上线、下线、状态变化、状态签名变化事件。 | `true` |
| `enable_world_tracking` | 是否追踪地图变化。关闭后不会产生地图切换事件。 | `true` |
| `vrchat_user_agent` | VRChat User-Agent。必须带上注册邮箱，否则可能登录失败。 | `AstrBotVRCFriendRadar/0.1.0 123@qq.com` |
| `login_session_timeout_seconds` | 登录交互超时秒数。发起登录后等待验证码的时限。 | `30` |
| `event_dedupe_window_seconds` | 事件去重窗口秒数。相同变化在窗口期内只推送一次。 | `300` |
| `event_batch_size` | 单次推送最大事件数。单轮检测最多合并多少条变化。 | `10` |
| `allow_auto_push` | 是否启用自动推送。关闭后仍可手动检测变化，但不会自动播报。 | `true` |
| `notify_location_detail` | 显示位置详细信息。用于更友好的位置文本格式化。 | `true` |
| `search_result_ttl_seconds` | 搜索结果有效期秒数，用于“按序号添加监控”。 | `120` |

---

## 命令说明

### 管理员命令

建议以下命令单独和机器人私聊配置

| 命令 | 说明 |
|---|---|
| `/vrc状态` | 查看插件运行状态、缓存数量、轮询次数、最近事件数等 |
| `/vrc测试` | 测试插件是否在线 |
| `/vrc推送测试` | 向通知群发送一条测试播报 |
| `/vrc登录 用户名 密码` | 登录 VRChat，建议私聊 Bot 使用 |
| `/vrc验证码 123456` | 提交邮箱验证码 / 动态验证码 / 恢复码 |
| `/vrc解绑登录` | 清除本地持久化登录态 |
| `/vrc同步好友` | 从 VRChat 拉取好友并写入本地缓存 |
| `/vrc搜索好友` | 搜索某位好友 |
| `/vrc添加监控序号 N` | 从最近一次好友搜索结果中按序号添加监控 |
| `/vrc好友列表 [页码]` | 查看缓存好友列表 |
| `/vrc在线好友 [页码]` | 查看当前缓存中的在线好友，显示世界名与实例类型 |
| `/vrc检测变化` | 立即执行一次变化检测 |
| `/vrc最近事件` | 查看最近变化事件 |
| `/vrc绑定通知群` | 在当前群执行，将该群绑定为通知群 |
| `/vrc解绑通知群` | 在当前群执行，将该群从通知群移除 |
| `/vrc通知群` | 查看通知群列表 |
| `/vrc添加监控 usr_xxx` | 按好友 ID 添加监控 |
| `/vrc删除监控 usr_xxx` | 按好友 ID 删除监控 |
| `/vrc监控列表` | 查看当前监控好友列表 |


### 所有人可用命令

| 命令 | 说明 |
|---|---|
| `/vrc搜索好友 关键词 [页码]` | 在本地好友缓存中模糊搜索 |
| `/vrc搜索地图 关键词` | 按地图名搜索 VRChat 世界 |

---

## 输出说明

### `/vrc在线好友`

在线好友列表会显示：

```text
1. 某好友 | 状态: active | 地图: Black Cat（好友实例）
```

其中：

- “Black Cat” 是世界名
- “好友实例 / 群组实例 / 私有实例 / 公开实例” 是根据位置字段格式化出的实例类型

### 地图切换播报

地图切换事件会优先尝试解析世界名，并缓存到本地：

```text
🗺️ 某好友 切换地图：Black Cat → Japan Shrine
```

---


## 推荐使用流程

```text
1. /vrc登录 用户名 密码
2. /vrc验证码 123456   （如有需要）
3. /vrc绑定通知群
4. /vrc同步好友
5. /vrc搜索好友 关键词
6. /vrc添加监控序号 1
7. 等待自动播报或手动执行 /vrc检测变化
```

---

## 数据存储

插件运行时会在数据目录生成并维护以下数据：

- `vrc_friend_radar.db`：SQLite 数据库，保存好友快照、事件历史、运行时设置
- `session.json`：登录态持久化文件
- `world_cache.json`：世界信息缓存

---

## 项目结构

```text
astrbot_plugin_vrc_friend_radar/
├─ main.py
├─ metadata.yaml
├─ README.md
├─ requirements.txt
├─ _conf_schema.json
├─ core/
│  ├─ config.py
│  ├─ db.py
│  ├─ diff.py
│  ├─ models.py
│  ├─ monitor.py
│  ├─ notifier.py
│  ├─ repository.py
│  ├─ search_state.py
│  ├─ session_store.py
│  ├─ utils.py
│  ├─ vrchat_client.py
│  └─ world_cache.py
└─ tests/
   └─ smoke_test.py
```

---

## 注意事项

- 请合理设置轮询间隔，避免过于频繁请求接口
- 建议私聊使用 `/vrc登录`，不要在群聊里直接发送账号密码
- 地图名解析依赖 VRChat API，首次解析某个世界时可能略慢，后续会命中本地缓存
- `/vrc好友列表` 与 `/vrc同步好友` 默认偏向轻量显示，`/vrc在线好友` 会显示更完整的“世界名 + 实例类型”

---
