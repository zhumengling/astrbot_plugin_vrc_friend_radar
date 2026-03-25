import time
from dataclasses import dataclass
from .models import FriendSnapshot


@dataclass(slots=True)
class SearchSession:
    session_key: str
    items: list[FriendSnapshot]
    created_at: float

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.created_at) > ttl_seconds
