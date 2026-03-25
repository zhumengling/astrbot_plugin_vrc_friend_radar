# astrbot_plugin_vrc_friend_radar

VRChat 好友状态监控插件，运行于 AstrBot。

> 用来监控 VRChat 好友的上线、下线、状态变化与地图切换，并把变化整理后推送到指定群聊。



## 功能特性

- VRChat 账号登录与二步验证码支持
- 登录态持久化恢复
- 好友列表同步与本地缓存
- 在线好友分页查看
- 好友搜索
- 地图搜索
- 好友状态变化检测
- 地图切换播报
- 地图名通过 VRChat API 自动解析
- 自动推送到通知群
- 去重与防刷屏
- 监控白名单管理
- 通知群管理
- 事件历史记录

---

## 适用场景

适合下面这些用途：

- 在群里关注固定 VRChat 好友是否上线
- 观察好友是否切换地图
- 只监控指定好友，避免全量刷屏
- 把状态变化作为群提醒或小型雷达系统使用

---

## 安装方式

将插件放入 AstrBot 的插件目录后，安装依赖并重载插件。

### 依赖

```text
vrchatapi
```

### 插件目录名

```text
astrbot_plugin_vrc_friend_radar
```

---

## 配置说明

插件支持通过配置文件或 AstrBot WebUI 配置以下内容：

- 轮询间隔
- 通知群列表
- 监控好友白名单
- 自动推送开关
- 去重窗口时间
- 单次推送上限
- 搜索结果保留时间
- VRChat User-Agent

---

## 命令说明

### 管理员可用

```text
/vrc状态
/vrc测试
/vrc推送测试
/vrc登录 用户名 密码
/vrc验证码 123456
/vrc解绑登录
/vrc同步好友
/vrc好友列表 [页码]
/vrc在线好友 [页码]
/vrc检测变化
/vrc最近事件
/vrc绑定通知群
/vrc解绑通知群
/vrc通知群
/vrc添加监控 usr_xxx
/vrc删除监控 usr_xxx
/vrc监控列表
/vrc添加监控序号 N
```

### 所有人可用

```text
/vrc搜索好友 关键词 [页码]
/vrc搜索地图 关键词
```

---

## 工作方式

### 1. 登录
插件登录 VRChat，并在本地保存可恢复的登录态。

### 2. 同步好友
插件拉取在线与离线好友，合并后写入本地缓存。

### 3. 检测变化
轮询时会将最新好友状态与缓存快照对比，识别变化事件。

### 4. 推送播报
若检测到变化，则会将同轮事件整理成一条合并播报并发送到通知群。

### 5. 地图名解析
遇到地图切换时，会根据世界 ID 调用 VRChat API 拉取地图名，并缓存结果。

---

## 推荐使用流程

```text
1. /vrc登录 用户名 密码
2. /vrc验证码 123456   （如有需要）
3. /vrc绑定通知群
4. /vrc同步好友
5. /vrc搜索好友 关键词
6. /vrc添加监控序号 1
7. 等待自动播报或手动 /vrc检测变化
```

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
- 建议优先监控真正需要关注的好友，减少无效推送
- 若准备公开仓库，不要提交本地数据库、缓存与登录态文件
- VRChat API 的可用性与返回字段可能会变化，必要时需做兼容调整

---

## 开源信息

MIT 风格开源整理建议可自行补充 License 文件。

如果你准备正式公开发布，建议同时补充：

- LICENSE
- 发布截图
- 更新日志
- 示例配置

---

## Repository

https://github.com/zhumengling/astrbot_plugin_vrc_friend_radar
