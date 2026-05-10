# VRChat 好友雷达 — Agent Skill

本文件是**面向 AstrBot LLM Agent** 的使用说明。当 AstrBot 载入本插件时会自动把下面列出的 FunctionTool 注册给 Agent，Agent 可以用自然语言直接调用，不需要用户敲命令。

普通用户无需阅读本文件；面向用户的命令手册请看 [README.md](../README.md)。

---

## 何时应该调用本插件的工具

当用户说到以下任何一类话题时，优先考虑调用对应工具：

| 用户说法（举例） | 应该调用的工具 |
| --- | --- |
| "Alice 在玩 VRChat 吗" / "她在线吗" | `vrc_friend_status` |
| "现在有谁在玩 VRChat" / "在线的有谁" | `vrc_online_friends` |
| "帮我查下 Bob 的资料" / "他的 bio 是什么" | `vrc_user_profile` |
| "我和 Bob 认识多久了" / "他有没有改过名" | `vrc_friend_history` |
| "今天 VRChat 大家都在哪个图" | `vrc_hot_worlds_today` |
| "XX 世界在哪搜" | `vrc_search_world` |
| "有谁在一起玩" / "同房情况" | `vrc_coroom_groups` |
| "那个实例还有多少人" | `vrc_instance_info` |
| "最近的动态有什么" / "他刚才干嘛" | `vrc_recent_events` |
| **"戳一下 Alice"** / **"boop 她"** | `vrc_boop` |
| **"帮我加 Bob 好友"** | `vrc_send_friend_request` |
| **"邀请 Carol 到我这个实例"** | `vrc_invite_user` |

> 这些工具的参数都接受自然语言的显示名，插件内部会做模糊匹配；匹配到多位好友时工具会返回让用户选择的提示，你把提示原样转给用户即可。

---

## 重要行为约束

1. **写操作（boop / send_friend_request / invite_user）只在用户明确提出时才调用**
   聊天里提到某人名字，不是戳对方或加好友的理由。必须等用户说出"戳一下"、"加 XX 好友"、"邀请 XX 过来"之类明确动作词才能调用。

2. **VRChat 的 Boop 没有文字消息**
   Boop 只能携带一个 emoji。如果用户说"戳 Alice 并告诉她 xxx"，不要把"xxx"当文字塞给 Boop；只能调用 `vrc_boop(query="Alice")` 并在回复中说明 Boop 不支持文字。

3. **`vrc_send_friend_request` 需要管理员开启 "公共加好友" 开关**
   如果工具返回 "管理员还没有开启..."，把原文转告用户，并提示管理员通过 `/vrc公共加好友 开启` 开启。

4. **需要先同步好友缓存**
   本地工具查不到人时，工具会提示 "可先执行 /vrc同步好友"。把这行提示原样转给用户，不要自己瞎猜。

5. **不要遍历调用工具**
   比如用户问 "XX 怎么样"，一次 `vrc_friend_status` 就够，不要连续 `vrc_user_profile`+`vrc_friend_history`+`vrc_instance_info` 全拉一遍。

6. **工具返回的文本已经是面向用户的友好格式**
   直接把工具返回的文字整合进你的回复即可，不用再二次加工成 JSON 或表格。

---

## 常用命令对照（给人看的备忘）

用户也可以直接用 slash 命令，工具只是把命令自动化了：

| 工具 | 等价命令 |
| --- | --- |
| `vrc_friend_status` | `/vrc在线好友` / `/vrc搜索好友` |
| `vrc_user_profile` | `/vrc资料 名字` |
| `vrc_friend_history` | `/vrc履历 名字` |
| `vrc_online_friends` | `/vrc在线好友` |
| `vrc_coroom_groups` | `/vrc同房情况` |
| `vrc_recent_events` | `/vrc最近事件` |
| `vrc_search_world` | `/vrc搜索地图 关键词` |
| `vrc_hot_worlds_today` | `/vrc热门世界` |
| `vrc_instance_info` | `/vrc实例 [目标]` |
| `vrc_boop` | `/vrc戳 名字 \| emojiId` |
| `vrc_send_friend_request` | `/vrc加好友 名字` |
| `vrc_invite_user` | `/vrc邀请 名字 \| worldId:instanceId` |

---

## 故障排查（Agent 可转给用户的提示）

- **"未登录"** → 管理员私聊 Bot 执行 `/vrc登录 用户名 密码`
- **"找不到该好友"** → 管理员执行 `/vrc同步好友` 后重试
- **Boop/邀请失败** → 目标可能不是好友，或不在线
- **灵魂画像中文乱码（Linux 容器）** → 把 NotoSansSC 字体放到插件 `assets/fonts/`
