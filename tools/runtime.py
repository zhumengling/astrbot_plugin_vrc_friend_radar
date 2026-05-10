"""Helpers used by FunctionTool implementations to format plugin data."""

from __future__ import annotations

import re
from typing import Any

from ..core.models import FriendSnapshot
from ..core.utils import format_location, infer_joinability


def find_snapshot_by_query(plugin: Any, query: str) -> tuple[FriendSnapshot | None, str]:
    keyword = str(query or '').strip()
    if not keyword:
        return None, '请提供待查询的好友名字或 ID。'
    snapshot_map = plugin.db.get_friend_snapshot_map()
    if re.fullmatch(r'usr_[A-Za-z0-9_-]+', keyword, flags=re.IGNORECASE):
        snapshot = snapshot_map.get(keyword)
        if snapshot is None:
            return None, f'未在本地缓存中找到用户 {keyword}，可先执行 /vrc同步好友。'
        return snapshot, ''

    lowered = keyword.casefold()
    exact_matches: list[FriendSnapshot] = []
    fuzzy_matches: list[FriendSnapshot] = []
    for snapshot in snapshot_map.values():
        display = plugin._sanitize_display_name_for_output(snapshot.display_name) or snapshot.friend_user_id
        if display.casefold() == lowered:
            exact_matches.append(snapshot)
        elif lowered in display.casefold():
            fuzzy_matches.append(snapshot)

    candidates = exact_matches or fuzzy_matches
    if not candidates:
        return None, f'本地缓存中没有包含 "{keyword}" 的好友。'
    if len(candidates) > 1:
        names = '、'.join(
            plugin._sanitize_display_name_for_output(item.display_name) or item.friend_user_id
            for item in candidates[:6]
        )
        return None, f'匹配到多位好友（{names}），请用更精确的名字或直接给出 usr_xxx ID。'
    return candidates[0], ''


async def format_location_and_joinability(plugin: Any, location: str | None) -> tuple[str, str]:
    world_text = await plugin._format_world_display(location)
    joinability = infer_joinability(location)
    return world_text, joinability


async def summarize_snapshot_line(plugin: Any, snapshot: FriendSnapshot) -> str:
    display = plugin._sanitize_display_name_for_output(snapshot.display_name) or snapshot.friend_user_id
    world_text = await plugin._format_world_display(snapshot.location)
    joinability = infer_joinability(snapshot.location, status=snapshot.status)
    status = snapshot.status or 'unknown'
    return f"{display} | 状态 {status} | 世界 {world_text} | {joinability}"


async def describe_snapshot(plugin: Any, snapshot: FriendSnapshot) -> str:
    display = plugin._sanitize_display_name_for_output(snapshot.display_name) or snapshot.friend_user_id
    status = (snapshot.status or 'unknown').strip() or 'unknown'
    if status.lower() == 'offline':
        return f"{display} 目前处于离线/仅 Web 在线。最近一次本地记录时间：{snapshot.updated_at or '未知'}。"
    world_text = await plugin._format_world_display(snapshot.location)
    joinability = infer_joinability(snapshot.location, status=snapshot.status)
    parts = [
        f"{display} 目前在线（{status}）",
        f"所在世界/实例：{world_text}",
        f"可加入性：{joinability}",
    ]
    if snapshot.status_description:
        parts.append(f"状态签名：{snapshot.status_description}")
    parts.append(f"（本地记录更新时间 {snapshot.updated_at or '未知'}）")
    return '；'.join(parts[:3]) + '。\n' + '\n'.join(parts[3:]) if len(parts) > 3 else '；'.join(parts) + '。'


def extract_world_id_safe(location: str | None) -> str:
    """Thin wrapper around core.utils.extract_world_id for tool-side use."""
    from ..core.utils import extract_world_id
    try:
        return extract_world_id(location)
    except Exception:
        return ''
