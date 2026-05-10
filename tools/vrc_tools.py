"""LLM FunctionTool definitions exposed by the VRChat Friend Radar plugin.

These tools let the AstrBot LLM Agent call VRChat Friend Radar features by
natural language, without the end user needing to type slash commands.

Two broad categories:

**Read-only queries** — safe to call eagerly:
- vrc_friend_status / vrc_user_profile / vrc_friend_history
- vrc_online_friends / vrc_coroom_groups / vrc_recent_events
- vrc_search_world / vrc_hot_worlds_today / vrc_instance_info

**Write actions** — only call when the user *explicitly* asks for it
(e.g. "戳一下 Alice"、"帮我向 Bob 发好友申请"、"邀请 Carol 到我现在这个实例"):
- vrc_boop — sends a VRChat boop (emoji only, no text)
- vrc_send_friend_request — sends a friend request
- vrc_invite_user — invites a friend to an instance

All tools return plain-text result strings; failures become friendly
messages rather than raised exceptions, so the Agent can always reply.

Register by calling ``build_llm_tools(plugin)`` inside
``VRCFriendRadarPlugin.initialize``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

from ..core.vrchat_client import VRChatRateLimitedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_limit(value: Any, default: int, *, lo: int = 1, hi: int = 20) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


async def _resolve_target(plugin: Any, query: str) -> tuple[str | None, str, str]:
    """返回 (friend_id, display_name, error_msg)。在本地缓存里做精确/模糊名字匹配。

    - 命中唯一结果 → 返回 friend_id 和规整后的 display_name
    - 找不到 / 多个候选 → friend_id=None，error_msg 给出提示
    """
    keyword = str(query or '').strip()
    if not keyword:
        return None, '', '请提供好友的显示名或 usr_xxx 用户 ID。'
    try:
        resolved = plugin._resolve_profile_target_candidates(keyword)
    except Exception as exc:
        return None, '', f'解析目标失败：{exc}'
    if resolved.friend_id:
        return resolved.friend_id, resolved.display_name or resolved.friend_id, ''
    if resolved.options:
        names = '、'.join(
            f'{opt.display_name}（{opt.friend_id}）'
            for opt in resolved.options[:5]
        )
        return None, '', f'"{keyword}" 匹配到多位好友（{names}）。请让用户更明确一些，或直接用 usr_xxx。'
    return None, '', f'在本地缓存中找不到与 "{keyword}" 匹配的好友。可先执行 /vrc同步好友。'


# ---------------------------------------------------------------------------
# 信息类（只读）工具
# ---------------------------------------------------------------------------

@dataclass
class VRCFriendStatusTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_friend_status'
    description: str = (
        '查询 VRChat 好友当前在线/离线状态、所在世界，以及能否加入的实例信息。'
        '传入好友的 display_name，也支持 usr_xxx 格式的 VRChat 用户 ID。'
        '调用前建议先同步过好友（/vrc同步好友），否则可能没有缓存。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'VRChat 好友的 display name 或 usr_xxx 用户 ID',
                },
            },
            'required': ['query'],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化，无法查询 VRChat 好友状态。'
        keyword = str(query or '').strip()
        if not keyword:
            return '请告诉我要查询的 VRChat 好友名字或 ID。'

        from .runtime import describe_snapshot, find_snapshot_by_query
        snapshot, reason = find_snapshot_by_query(plugin, keyword)
        if snapshot is None:
            return reason or f'在本地缓存中找不到与 "{keyword}" 匹配的好友，可以先执行 /vrc同步好友。'
        return await describe_snapshot(plugin, snapshot)


@dataclass
class VRCOnlineFriendsTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_online_friends'
    description: str = (
        '列出当前缓存中在线的 VRChat 好友及所在世界。结果来自本地缓存，'
        '可能略滞后于真实状态。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'limit': {
                    'type': 'number',
                    'description': '最多返回的好友条目数，默认 10，最大 20。',
                },
            },
            'required': [],
        }
    )

    async def run(self, event: AstrMessageEvent, limit: Any = 10):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        safe_limit = _safe_limit(limit, 10, lo=1, hi=20)
        snapshots = plugin.monitor.list_online_cached_friends(limit=safe_limit, offset=0)
        if not snapshots:
            return '当前没有缓存到的在线好友。可以让管理员执行 /vrc同步好友 后重试。'
        from .runtime import summarize_snapshot_line
        lines = []
        for snapshot in snapshots:
            lines.append(await summarize_snapshot_line(plugin, snapshot))
        return '当前在线好友：\n' + '\n'.join(lines)


@dataclass
class VRCSearchWorldTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_search_world'
    description: str = (
        '按关键词搜索 VRChat 世界，返回世界名、作者、世界 ID。'
        '该操作会请求 VRChat API，建议仅在需要最新世界信息时使用。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'keyword': {
                    'type': 'string',
                    'description': '世界关键词，支持英文/日文/中文。',
                },
                'limit': {
                    'type': 'number',
                    'description': '返回世界数量，默认 5，最大 10。',
                },
            },
            'required': ['keyword'],
        }
    )

    async def run(self, event: AstrMessageEvent, keyword: str, limit: Any = 5):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        keyword_text = str(keyword or '').strip()
        if not keyword_text:
            return '请告诉我要搜索的世界关键词。'
        safe_limit = _safe_limit(limit, 5, lo=1, hi=10)
        if not plugin.monitor.client.is_logged_in():
            return '机器人当前未登录 VRChat，无法搜索世界。'
        try:
            results = await plugin.monitor.client.search_worlds(keyword_text, limit=safe_limit, offset=0)
        except Exception as exc:
            logger.warning(f'[vrc_friend_radar][tool] search_worlds failed: {exc}')
            return f'搜索世界失败：{exc}'
        if not results:
            return f'没有找到与 "{keyword_text}" 相关的世界。'
        lines = [f'搜索到 {len(results)} 个世界：']
        for idx, item in enumerate(results, start=1):
            lines.append(
                f"{idx}. {item.get('name') or '未命名世界'} | 作者 {item.get('author_name') or '未知'} | ID {item.get('id') or ''}"
            )
        return '\n'.join(lines)


@dataclass
class VRCHotWorldsTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_hot_worlds_today'
    description: str = (
        '查询今天（0 点至今）监控好友在 VRChat 中最常出现的世界 Top 榜。'
        '返回世界名、热度、涉及好友数、实例可加入性概览。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'top_n': {
                    'type': 'number',
                    'description': '取前 N 个世界，默认 5，最大 10。',
                },
            },
            'required': [],
        }
    )

    async def run(self, event: AstrMessageEvent, top_n: Any = 5):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        safe_top = _safe_limit(top_n, 5, lo=1, hi=10)
        try:
            items = await plugin._collect_hot_world_stats_today(top_n=safe_top)
        except Exception as exc:
            logger.warning(f'[vrc_friend_radar][tool] hot_worlds failed: {exc}')
            return f'统计热门世界失败：{exc}'
        if not items:
            return '今日暂无热门世界统计（还没有上线过的监控好友）。'
        lines = [f'今日 VRChat 热门世界 Top {safe_top}：']
        for idx, item in enumerate(items, start=1):
            overview = plugin._format_joinability_overview(item['joinability'])
            lines.append(
                f"{idx}. {item['world_name']} | 热度 {item['count']} | 涉及好友 {item['friend_count']} | {overview}"
            )
        return '\n'.join(lines)


@dataclass
class VRCCoroomTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_coroom_groups'
    description: str = (
        '列出当前监控好友里正在同一个 VRChat 实例的分组（即同房情况），'
        '每组包含世界名、人数、成员名字、实例可加入性。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {},
            'required': [],
        }
    )

    async def run(self, event: AstrMessageEvent):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        groups = plugin.monitor.list_coroom_groups()
        if not groups:
            return '当前没有监控好友同处同一实例。'
        from .runtime import format_location_and_joinability
        lines = [f'当前同房分组 {len(groups)} 个：']
        for idx, group in enumerate(groups, start=1):
            members = group.get('members') or []
            names = [plugin._sanitize_display_name_for_output(m.display_name) for m in members]
            location_key = group.get('location_key') or ''
            world_text, joinability = await format_location_and_joinability(plugin, location_key)
            lines.append(
                f"{idx}. {world_text} | 人数 {len(members)} | {joinability} | 成员 {'、'.join(names)}"
            )
        return '\n'.join(lines)


@dataclass
class VRCUserProfileTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_user_profile'
    description: str = (
        '查看 VRChat 用户的公开资料：显示名、状态、签名、bio、当前位置、加入日期、标签等。'
        '支持模糊匹配好友名字或直接 usr_xxx。对不是好友的用户，如果有 usr_xxx 也能查。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'VRChat 显示名或 usr_xxx 用户 ID',
                },
            },
            'required': ['query'],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        if not plugin.monitor.client.is_logged_in():
            return '机器人当前未登录 VRChat，无法拉取用户资料。'

        keyword = str(query or '').strip()
        # 先尝试本地名字匹配；如果直接传了 usr_xxx 也允许绕过本地缓存
        target = ''
        display_hint = keyword
        if re.fullmatch(r'usr_[A-Za-z0-9_-]+', keyword, flags=re.IGNORECASE):
            target = keyword
        else:
            friend_id, display_name, err = await _resolve_target(plugin, keyword)
            if friend_id is None:
                return err or '未找到目标用户。'
            target = friend_id
            display_hint = display_name

        try:
            info = await plugin.monitor.client.get_user_detail(target)
        except Exception as exc:
            return f'查看资料失败：{exc}'
        if not info:
            return f'未获取到 {display_hint} 的公开资料。'

        lines = [f"{info.get('display_name') or display_hint} 的 VRChat 资料："]
        lines.append(f"用户 ID：{info.get('id') or target}")
        if info.get('status'):
            bits = [f"状态：{info['status']}"]
            if info.get('status_description'):
                bits.append(f"签名：{info['status_description']}")
            lines.append(' | '.join(bits))
        if info.get('location'):
            world_text = await plugin._format_world_display(info['location'])
            lines.append(f"当前位置：{world_text}")
        if info.get('last_platform'):
            lines.append(f"最近平台：{info['last_platform']}")
        if info.get('date_joined'):
            lines.append(f"加入日期：{info['date_joined']}")
        if info.get('is_friend'):
            lines.append('与机器人账号互为好友')
        if info.get('bio'):
            bio = info['bio'].strip()
            if len(bio) > 160:
                bio = bio[:160] + '…'
            lines.append(f"简介：{bio}")
        if info.get('tags'):
            friendly = [t for t in info['tags'] if not t.startswith('system_')][:8]
            if friendly:
                lines.append('标签：' + '、'.join(friendly))
        return '\n'.join(lines)


@dataclass
class VRCFriendshipHistoryTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_friend_history'
    description: str = (
        '查看本地记录的一位 VRChat 好友的履历：初次发现日期、认识多少天、改名历史、本地备注、当前标签。'
        '数据完全来自本地缓存，不请求 VRChat API。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'VRChat 显示名或 usr_xxx 用户 ID',
                },
            },
            'required': ['query'],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        friend_id, display_name, err = await _resolve_target(plugin, query)
        if friend_id is None:
            return err

        from datetime import datetime
        profile = plugin.db.get_friend_profile(friend_id)
        history = plugin.db.list_friend_name_history(friend_id, limit=10)
        note = plugin.db.get_friend_note(friend_id)
        tags = plugin.monitor.get_friend_tags(friend_id)

        lines = [f"{display_name}（{friend_id}）的履历"]
        if profile and profile.get('first_seen_at'):
            fs = profile['first_seen_at']
            try:
                days = max(0, (datetime.now() - datetime.fromisoformat(fs)).days)
                lines.append(f"初次发现：{fs}（认识约 {days} 天）")
            except Exception:
                lines.append(f"初次发现：{fs}")
        else:
            lines.append('初次发现：暂无记录')
        if tags:
            lines.append('标签：' + '、'.join(tags))
        if note and note.get('note_text'):
            lines.append(f"备注：{note['note_text']}")
        if history:
            lines.append('改名历史：')
            for item in history:
                old_name = item.get('old_display_name') or '(首次记录)'
                new_name = item.get('new_display_name')
                when = item.get('changed_at') or ''
                lines.append(f"- {when}: {old_name} → {new_name}")
        else:
            lines.append('改名历史：暂无记录')
        return '\n'.join(lines)


@dataclass
class VRCInstanceInfoTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_instance_info'
    description: str = (
        '查询一个 VRChat 实例的人数、容量、区域、owner、是否已满等信息。'
        '可以传好友名字（查对方所在实例）、usr_xxx、或者完整的 worldId:instanceId。'
        '不传参数则查询机器人当前所在实例。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': '好友名字 / usr_xxx / wrld_xxx:instanceId；留空则查机器人自己的实例',
                },
            },
            'required': [],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str = ''):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        if not plugin.monitor.client.is_logged_in():
            return '机器人当前未登录 VRChat，无法查询实例。'

        keyword = str(query or '').strip()
        world_id = ''
        instance_id = ''

        from .runtime import extract_world_id_safe
        if keyword.startswith('wrld_') and ':' in keyword:
            world_part, inst_part = keyword.split(':', 1)
            world_id = world_part.strip()
            instance_id = inst_part.split('~', 1)[0].strip()
        elif keyword:
            # 按好友名解析
            friend_id, _name, err = await _resolve_target(plugin, keyword)
            if friend_id is None:
                return err
            snap = plugin.db.get_friend_snapshot_map().get(friend_id)
            if not snap or not snap.location:
                return f'{keyword} 当前不在任何实例中。'
            world_id = extract_world_id_safe(snap.location)
            if ':' in (snap.location or ''):
                instance_id = snap.location.split(':', 1)[1].split('~', 1)[0].strip()
        else:
            # 机器人自己
            try:
                self_snap = await plugin.monitor.client.fetch_self_snapshot()
            except Exception:
                self_snap = None
            if not self_snap or not self_snap.location:
                return '机器人当前不在任何实例中。'
            world_id = extract_world_id_safe(self_snap.location)
            if ':' in (self_snap.location or ''):
                instance_id = self_snap.location.split(':', 1)[1].split('~', 1)[0].strip()

        if not world_id or not instance_id:
            return '没有解析出有效的 worldId + instanceId。'

        try:
            info = await plugin.monitor.client.get_instance(world_id, instance_id)
        except Exception as exc:
            return f'查询实例失败：{exc}'
        if not info:
            return '未能查到该实例（可能已经关闭）。'

        world_name = await plugin._get_world_name(f'{world_id}:{instance_id}')
        lines = [f"{world_name}"]
        lines.append(f"实例 ID：{instance_id}")
        capacity = info.get('capacity') or 0
        n_users = info.get('n_users') or 0
        if capacity:
            lines.append(f"人数：{n_users}/{capacity}" + ('（已满）' if info.get('full') else ''))
        else:
            lines.append(f"人数：{n_users}")
        if info.get('region'):
            lines.append(f"区域：{info['region']}")
        if info.get('access_type'):
            lines.append(f"类型：{info['access_type']}")
        return '\n'.join(lines)


@dataclass
class VRCRecentEventsTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_recent_events'
    description: str = (
        '列出最近的好友动态事件（上线/下线/状态变更/切图/同房/改名）。'
        '可以带 query 参数聚焦到某一位好友。数据来自本地事件日志。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': '可选。聚焦到某一位好友的名字或 usr_xxx；留空则展示所有监控好友的事件。',
                },
                'limit': {
                    'type': 'number',
                    'description': '返回事件数量，默认 10，最大 20。',
                },
            },
            'required': [],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str = '', limit: Any = 10):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        safe_limit = _safe_limit(limit, 10, lo=1, hi=20)
        events = plugin.monitor.list_recent_events(limit=safe_limit * 3)  # 稍多拉一点给过滤留空间
        if not events:
            return '当前没有事件历史。'

        target_id = ''
        if query:
            friend_id, _name, _err = await _resolve_target(plugin, query)
            if friend_id:
                target_id = friend_id

        picked = []
        for item in events:
            if target_id and item.friend_user_id != target_id and item.event_type != 'co_room':
                continue
            picked.append(item)
            if len(picked) >= safe_limit:
                break
        if not picked:
            return '没有匹配的事件记录。'

        lines = ['最近事件：']
        snapshot_map = plugin.db.get_friend_snapshot_map()
        for item in picked:
            snap = snapshot_map.get(item.friend_user_id)
            name = plugin._sanitize_display_name_for_output(snap.display_name) if snap else item.friend_user_id
            old_v = item.old_value or '空'
            new_v = item.new_value or '空'
            lines.append(f"{item.created_at} | {item.event_type} | {name} | {old_v} → {new_v}")
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 写操作工具 —— 只在用户明确提出时才应该被调用
# ---------------------------------------------------------------------------

@dataclass
class VRCBoopTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_boop'
    description: str = (
        '向一位 VRChat 好友发送 Boop（戳一下）互动。'
        '**仅当用户明确要求"戳"、"boop"、"捅一下"等动作时才调用，不要因为聊天里提到对方名字就主动戳。**'
        'VRChat 的 Boop 只能携带一个 emoji，不支持文字留言。'
        'emoji_id 可选：留空=纯戳（对方只会收到"被戳"通知）；'
        '也可以填官方默认 emoji 的常量名（如 smile / skull / ghost），或上传后的自定义贴纸 FileID。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': '要戳的好友，支持 display name 或 usr_xxx',
                },
                'emoji_id': {
                    'type': 'string',
                    'description': '可选。VRChat 内置 emoji 常量名或 FileID。不确定时留空。',
                },
            },
            'required': ['query'],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str, emoji_id: str = ''):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        if not plugin.monitor.client.is_logged_in():
            return '机器人当前未登录 VRChat，无法戳一戳。'
        friend_id, display_name, err = await _resolve_target(plugin, query)
        if friend_id is None:
            return err
        try:
            await plugin.monitor.client.boop_user(friend_id, emoji_id or None)
        except VRChatRateLimitedError as exc:
            wait = exc.retry_after_seconds or 60
            return (
                f'[RATE_LIMITED] 戳 {display_name} 被 VRChat 限流，需要等 {wait} 秒。'
                f'不要立刻重试本工具，请直接把这条消息转告用户。'
            )
        except Exception as exc:
            return f'戳一戳失败：{exc}'
        suffix = f"，带上了 emoji：{emoji_id}" if emoji_id else '（纯戳，没带 emoji）'
        return f'已经戳了 {display_name}（{friend_id}）一下{suffix}。'


@dataclass
class VRCSendFriendRequestTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_send_friend_request'
    description: str = (
        '让机器人账号向目标 VRChat 用户发送好友申请。'
        '**仅当用户明确要求"加好友"时才调用**；不要因为聊天里提到某人就主动加。'
        '需要管理员通过 /vrc公共加好友 开启 开启"公共加好友"开关，否则会被拒绝。'
        '目标可以是好友名字或 usr_xxx 用户 ID。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': '目标用户：display name 或 usr_xxx',
                },
            },
            'required': ['query'],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        if not plugin._is_public_friend_request_allowed():
            return '管理员还没有开启"公共加好友"功能，无法通过 AI 触发加好友。'
        if not plugin.monitor.client.is_logged_in():
            return '机器人当前未登录 VRChat，无法发送好友申请。'

        keyword = str(query or '').strip()
        # 允许直接传 usr_xxx 绕过本地缓存
        target_id = ''
        display_hint = keyword
        if re.fullmatch(r'usr_[A-Za-z0-9_-]+', keyword, flags=re.IGNORECASE):
            target_id = keyword
        else:
            friend_id, display_name, err = await _resolve_target(plugin, keyword)
            if friend_id is None:
                return err
            target_id = friend_id
            display_hint = display_name

        try:
            await plugin.monitor.client.send_friend_request(target_id)
        except Exception as exc:
            return f'发送好友申请失败：{exc}'
        return f'已向 {display_hint}（{target_id}）发送好友申请。'


@dataclass
class VRCInviteUserTool(FunctionTool):
    plugin: Any = None
    name: str = 'vrc_invite_user'
    description: str = (
        '让机器人账号向一位 VRChat 好友发送实例邀请（邀请到指定世界/实例）。'
        '**仅当用户明确要求"邀请 XX 过来"或"让 XX 到我这个实例"时才调用。**'
        'instance_id 可选：留空时默认邀请到机器人当前所在的实例；'
        '也可以传完整的 "wrld_xxx:12345~public" 地址指定具体实例。'
    )
    parameters: dict = field(
        default_factory=lambda: {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': '要邀请的好友：display name 或 usr_xxx',
                },
                'instance_id': {
                    'type': 'string',
                    'description': '可选。完整 worldId:instanceId，留空则用机器人当前实例。',
                },
            },
            'required': ['query'],
        }
    )

    async def run(self, event: AstrMessageEvent, query: str, instance_id: str = ''):
        plugin = self.plugin
        if plugin is None:
            return '插件未正确初始化。'
        if not plugin.monitor.client.is_logged_in():
            return '机器人当前未登录 VRChat，无法邀请。'
        friend_id, display_name, err = await _resolve_target(plugin, query)
        if friend_id is None:
            return err

        location = str(instance_id or '').strip()
        if not location:
            try:
                self_snap = await plugin.monitor.client.fetch_self_snapshot()
            except Exception:
                self_snap = None
            location = self_snap.location if self_snap else ''
            if not location:
                return '无法识别机器人当前实例。请让用户直接传入 worldId:instanceId。'
        try:
            await plugin.monitor.client.invite_user_to_instance(friend_id, location)
        except Exception as exc:
            return f'发送邀请失败：{exc}'
        return f'已向 {display_name}（{friend_id}）发送实例邀请。目标实例：{location}'


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

def build_llm_tools(plugin: Any) -> list[FunctionTool]:
    """Build the list of FunctionTool instances bound to this plugin."""
    return [
        # 信息查询
        VRCFriendStatusTool(plugin=plugin),
        VRCUserProfileTool(plugin=plugin),
        VRCFriendshipHistoryTool(plugin=plugin),
        VRCOnlineFriendsTool(plugin=plugin),
        VRCCoroomTool(plugin=plugin),
        VRCRecentEventsTool(plugin=plugin),
        VRCSearchWorldTool(plugin=plugin),
        VRCHotWorldsTool(plugin=plugin),
        VRCInstanceInfoTool(plugin=plugin),
        # 写操作（需要用户明确意图）
        VRCBoopTool(plugin=plugin),
        VRCSendFriendRequestTool(plugin=plugin),
        VRCInviteUserTool(plugin=plugin),
    ]
