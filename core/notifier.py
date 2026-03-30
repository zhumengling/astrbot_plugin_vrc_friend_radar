from .models import RadarEvent
from .utils import format_location, infer_joinability


class Notifier:
    def build_message(self, event: RadarEvent) -> str:
        if event.event_type == "friend_online":
            return f"🟢 {event.display_name} 上线啦 | 状态：{event.new_value or 'unknown'}"
        if event.event_type == "friend_offline":
            return f"⚫ {event.display_name} 下线了"
        if event.event_type == "status_changed":
            return f"🟡 {event.display_name} 状态变化：{event.old_value or 'unknown'} → {event.new_value or 'unknown'}"
        if event.event_type == "location_changed":
            old_joinability = infer_joinability(event.old_value)
            new_joinability = infer_joinability(event.new_value)
            return f"🗺️ {event.display_name} 切换地图：{format_location(event.old_value)} → {format_location(event.new_value)} | {old_joinability} → {new_joinability}"
        if event.event_type == "status_message_changed":
            return f"✏️ {event.display_name} 状态签名变化：{event.old_value or '空'} → {event.new_value or '空'}"
        if event.event_type == "co_room":
            joinability = infer_joinability(event.friend_user_id)
            return f"👥 同房提醒：{event.display_name} 正在同一实例 | {joinability}"
        return f"ℹ️ {event.display_name} 发生未知变化"

    def build_location_change_message(self, display_name: str, old_world_name: str, new_world_name: str, old_location: str | None, new_location: str | None, status: str | None = None) -> str:
        old_joinability = infer_joinability(old_location)
        new_joinability = infer_joinability(new_location, status=status)
        return f"🗺️ {display_name} 切换地图：{old_world_name} → {new_world_name} | {old_joinability} → {new_joinability}"

    def build_coroom_message(self, world_display: str, count: int, names: list[str], joinability: str) -> str:
        joined_names = '、'.join(names)
        return f"👥 同房提醒：{count} 位监控好友同处 {world_display} | 成员：{joined_names} | {joinability}"

    def build_batch_message(self, messages: list[str]) -> str:
        if not messages:
            return ""
        lines = ["📢 VRChat 好友动态播报"]
        for msg in messages:
            lines.append(f"- {msg}")
        return "\n".join(lines)
