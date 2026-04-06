from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import RadarEvent


@dataclass(slots=True)
class StandardEvent:
    event_id: str
    friend_user_id: str
    event_type: str
    old_value: str | None
    new_value: str | None
    happened_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)



def standard_event_from_radar(event: RadarEvent) -> StandardEvent:
    happened_at = event.created_at
    if isinstance(happened_at, str):
        try:
            happened_at = datetime.fromisoformat(happened_at)
        except ValueError:
            happened_at = datetime.now()
    elif not isinstance(happened_at, datetime):
        happened_at = datetime.now()

    event_id = f"{event.friend_user_id}:{event.event_type}:{event.old_value}:{event.new_value}:{happened_at.isoformat(timespec='seconds')}"
    return StandardEvent(
        event_id=event_id,
        friend_user_id=event.friend_user_id,
        event_type=event.event_type,
        old_value=event.old_value,
        new_value=event.new_value,
        happened_at=happened_at,
        metadata={},
    )
