from .models import FriendSnapshot, RadarEvent


def diff_snapshot(old: FriendSnapshot, new: FriendSnapshot) -> list[RadarEvent]:
    events: list[RadarEvent] = []

    old_status = (old.status or "").lower()
    new_status = (new.status or "").lower()
    old_location = old.location or ""
    new_location = new.location or ""

    if old_status != new_status:
        if old_status == "offline" and new_status != "offline":
            event_type = "friend_online"
        elif old_status != "offline" and new_status == "offline":
            event_type = "friend_offline"
        else:
            event_type = "status_changed"
        events.append(
            RadarEvent(
                friend_user_id=new.friend_user_id,
                display_name=new.display_name,
                event_type=event_type,
                old_value=old.status,
                new_value=new.status,
                created_at=new.updated_at,
            )
        )

    if old_location != new_location:
        events.append(
            RadarEvent(
                friend_user_id=new.friend_user_id,
                display_name=new.display_name,
                event_type="location_changed",
                old_value=old.location,
                new_value=new.location,
                created_at=new.updated_at,
            )
        )

    if (old.status_description or "") != (new.status_description or ""):
        events.append(
            RadarEvent(
                friend_user_id=new.friend_user_id,
                display_name=new.display_name,
                event_type="status_message_changed",
                old_value=old.status_description,
                new_value=new.status_description,
                created_at=new.updated_at,
            )
        )

    return events
