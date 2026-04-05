from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_DAILY_TASK_TIME = "21:00"


@dataclass(slots=True)
class PluginConfig:
    raw_config: Any
    context: Any
    poll_interval_seconds: int = 180
    notify_group_ids: list[str] = field(default_factory=list)
    watch_friend_ids: list[str] = field(default_factory=list)
    watch_self: bool = False
    enable_status_tracking: bool = True
    enable_world_tracking: bool = True
    vrchat_user_agent: str = "AstrBotVRCFriendRadar/0.1.0"
    login_session_timeout_seconds: int = 30
    event_dedupe_window_seconds: int = 300
    event_batch_size: int = 10
    allow_auto_push: bool = True
    notify_location_detail: bool = True
    search_result_ttl_seconds: int = 120
    coroom_notify_interval_seconds: int = 600
    coroom_notify_min_members: int = 2
    coroom_notify_joinable_only: bool = False
    enable_daily_report: bool = False
    daily_task_time: str = DEFAULT_DAILY_TASK_TIME
    daily_report_time: str = DEFAULT_DAILY_TASK_TIME
    daily_report_top_n: int = 5
    world_translation_cache_max_entries: int = 500

    def __init__(self, raw_config: Any, context: Any):
        self.raw_config = raw_config
        self.context = context
        self.poll_interval_seconds = max(60, self._read_int("poll_interval_seconds", 180))
        self.notify_group_ids = self._normalize_str_list(self._read_list("notify_group_ids", []))
        self.watch_friend_ids = self._normalize_str_list(self._read_list("watch_friend_ids", []))
        self.watch_self = self._read_bool("watch_self", False)
        self.enable_status_tracking = self._read_bool("enable_status_tracking", True)
        self.enable_world_tracking = self._read_bool("enable_world_tracking", True)
        self.vrchat_user_agent = str(
            self._read(
                "vrchat_user_agent",
                "AstrBotVRCFriendRadar/0.1.0",
            )
        ).strip()
        self.login_session_timeout_seconds = max(30, min(600, self._read_int("login_session_timeout_seconds", 30)))
        self.event_dedupe_window_seconds = max(30, self._read_int("event_dedupe_window_seconds", 300))
        self.event_batch_size = max(1, min(50, self._read_int("event_batch_size", 10)))
        self.allow_auto_push = self._read_bool("allow_auto_push", True)
        self.notify_location_detail = self._read_bool("notify_location_detail", True)
        self.search_result_ttl_seconds = max(30, self._read_int("search_result_ttl_seconds", 120))
        self.coroom_notify_interval_seconds = max(30, self._read_int("coroom_notify_interval_seconds", 600))
        self.coroom_notify_min_members = max(2, self._read_int("coroom_notify_min_members", 2))
        self.coroom_notify_joinable_only = self._read_bool("coroom_notify_joinable_only", False)
        self.enable_daily_report = self._read_bool("enable_daily_report", False)

        self.daily_task_time = self._normalize_hhmm(str(self._read("daily_task_time", DEFAULT_DAILY_TASK_TIME)))
        # 兼容旧配置：daily_report_time 仍可单独覆盖日报任务时间
        if self._has_key("daily_report_time"):
            self.daily_report_time = self._normalize_hhmm(str(self._read("daily_report_time", self.daily_task_time)))
        else:
            self.daily_report_time = self.daily_task_time

        self.daily_report_top_n = max(1, min(20, self._read_int("daily_report_top_n", 5)))
        self.world_translation_cache_max_entries = max(0, self._read_int("world_translation_cache_max_entries", 500))

    def _has_key(self, key: str) -> bool:
        cfg = self.raw_config
        if isinstance(cfg, dict):
            return key in cfg
        if hasattr(cfg, "keys"):
            try:
                return key in cfg.keys()
            except Exception:
                pass
        if hasattr(cfg, "__contains__"):
            try:
                return key in cfg
            except Exception:
                pass
        return hasattr(cfg, key)

    def _read(self, key: str, default: Any):
        if hasattr(self.raw_config, "get"):
            try:
                return self.raw_config.get(key, default)
            except TypeError:
                pass
        return getattr(self.raw_config, key, default)

    def _read_int(self, key: str, default: int) -> int:
        value = self._read(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _read_bool(self, key: str, default: bool) -> bool:
        value = self._read(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        return bool(value)

    def _read_list(self, key: str, default: list[str]) -> list[str]:
        value = self._read(key, default)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, tuple):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            parts = text.replace('|', ',').replace('，', ',').split(',')
            return [item.strip() for item in parts if item.strip()]
        return list(default)

    @staticmethod
    def _normalize_str_list(items: list[str] | tuple[str, ...] | None) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items or []:
            value = str(item or '').strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _try_write_raw_list(self, key: str, items: list[str]) -> bool:
        payload = list(items)
        cfg = self.raw_config
        try:
            if isinstance(cfg, dict):
                cfg[key] = payload
                return True
            if hasattr(cfg, "__setitem__"):
                try:
                    cfg[key] = payload
                    return True
                except Exception:
                    pass
            setattr(cfg, key, payload)
            return True
        except Exception:
            return False

    def has_notify_group_ids_key(self) -> bool:
        return self._has_key("notify_group_ids")

    def has_watch_friend_ids_key(self) -> bool:
        return self._has_key("watch_friend_ids")

    def read_notify_group_ids_from_raw(self) -> list[str]:
        # 注意：这里缺省值必须是 []，不能回退到 runtime list；否则 WebUI 删除到空时会被旧值回填
        return self._normalize_str_list(self._read_list("notify_group_ids", []))

    def read_watch_friend_ids_from_raw(self) -> list[str]:
        # 注意：这里缺省值必须是 []，不能回退到 runtime list；否则 WebUI 删除到空时会被旧值回填
        return self._normalize_str_list(self._read_list("watch_friend_ids", []))

    def sync_runtime_lists(
        self,
        notify_group_ids: list[str] | None = None,
        watch_friend_ids: list[str] | None = None,
        write_back_raw: bool = True,
    ) -> None:
        if notify_group_ids is not None:
            normalized_notify = self._normalize_str_list(notify_group_ids)
            self.notify_group_ids = normalized_notify
            if write_back_raw:
                self._try_write_raw_list("notify_group_ids", normalized_notify)

        if watch_friend_ids is not None:
            normalized_watch = self._normalize_str_list(watch_friend_ids)
            self.watch_friend_ids = normalized_watch
            if write_back_raw:
                self._try_write_raw_list("watch_friend_ids", normalized_watch)

    def _normalize_hhmm(self, value: str, fallback: str = DEFAULT_DAILY_TASK_TIME) -> str:
        text = (value or '').strip()
        if ':' not in text:
            return fallback
        hh, mm = text.split(':', 1)
        try:
            h = int(hh)
            m = int(mm)
        except ValueError:
            return fallback
        if h < 0 or h > 23 or m < 0 or m > 59:
            return fallback
        return f"{h:02d}:{m:02d}"

    def get_daily_task_time(self, task_name: str) -> str:
        if task_name == "daily_report":
            return self.daily_report_time or self.daily_task_time
        return self.daily_task_time

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
