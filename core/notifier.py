from .models import RadarEvent
from .utils import format_location


class Notifier:
    def build_message(self, event: RadarEvent) -> str:
        if event.event_type == "friend_online":
            return f"🟢 {event.display_name} 上线啦 | 状态：{event.new_value or 'unknown'}"
        if event.event_type == "friend_offline":
            return f"⚫ {event.display_name} 下线了"
        if event.event_type == "status_changed":
            return f"🟡 {event.display_name} 状态变化：{event.old_value or 'unknown'} → {event.new_value or 'unknown'}"
        if event.event_type == "location_changed":
            return f"🗺️ {event.display_name} 切换地图：{format_location(event.old_value)} → {format_location(event.new_value)}"
        if event.event_type == "status_message_changed":
            return f"✏️ {event.display_name} 状态签名变化：{event.old_value or '空'} → {event.new_value or '空'}"
        return f"ℹ️ {event.display_name} 发生未知变化"

    def build_batch_message(self, messages: list[str]) -> str:
        if not messages:
            return ""
        lines = ["📢 VRChat 好友动态播报"]
        for msg in messages:
            lines.append(f"- {msg}")
        return "\n".join(lines)
