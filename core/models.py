from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class FriendSnapshot:
    friend_user_id: str
    display_name: str
    status: Optional[str] = None
    location: Optional[str] = None
    updated_at: str = ""
    status_description: Optional[str] = None


@dataclass(slots=True)
class RadarEvent:
    friend_user_id: str
    display_name: str
    event_type: str
    old_value: str | None = None
    new_value: str | None = None
    created_at: str = ""
