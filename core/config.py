from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PluginConfig:
    raw_config: Any
    context: Any
    poll_interval_seconds: int = 90
    notify_group_ids: list[str] = field(default_factory=list)
    watch_friend_ids: list[str] = field(default_factory=list)
    enable_status_tracking: bool = True
    enable_world_tracking: bool = True
    vrchat_user_agent: str = "AstrBotVRCFriendRadar/0.1.0 contact@example.com"
    login_session_timeout_seconds: int = 60
    event_dedupe_window_seconds: int = 300
    event_batch_size: int = 10
    allow_auto_push: bool = True
    notify_location_detail: bool = True
    search_result_ttl_seconds: int = 120

    def __init__(self, raw_config: Any, context: Any):
        self.raw_config = raw_config
        self.context = context
        self.poll_interval_seconds = max(60, int(self._read("poll_interval_seconds", 90)))
        self.notify_group_ids = list(self._read("notify_group_ids", []))
        self.watch_friend_ids = list(self._read("watch_friend_ids", []))
        self.enable_status_tracking = bool(self._read("enable_status_tracking", True))
        self.enable_world_tracking = bool(self._read("enable_world_tracking", True))
        self.vrchat_user_agent = str(
            self._read(
                "vrchat_user_agent",
                "AstrBotVRCFriendRadar/0.1.0 contact@example.com",
            )
        ).strip()
        self.login_session_timeout_seconds = max(30, int(self._read("login_session_timeout_seconds", 60)))
        self.event_dedupe_window_seconds = max(30, int(self._read("event_dedupe_window_seconds", 300)))
        self.event_batch_size = max(1, int(self._read("event_batch_size", 10)))
        self.allow_auto_push = bool(self._read("allow_auto_push", True))
        self.notify_location_detail = bool(self._read("notify_location_detail", True))
        self.search_result_ttl_seconds = max(30, int(self._read("search_result_ttl_seconds", 120)))

    def _read(self, key: str, default: Any):
        if hasattr(self.raw_config, "get"):
            try:
                return self.raw_config.get(key, default)
            except TypeError:
                pass
        return getattr(self.raw_config, key, default)

    @property
    def plugin_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def data_dir(self) -> Path:
        path = self.plugin_dir / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vrc_friend_radar.db"
