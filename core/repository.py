import sqlite3
from .config import PluginConfig
from .models import FriendSnapshot


class SettingsRepository:
    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg

    def initialize(self) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS plugin_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)")
            conn.commit()
        finally:
            conn.close()

    def _ensure_table(self) -> None:
        self.initialize()

    def _get_raw(self, key: str) -> str | None:
        self._ensure_table()
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            row = conn.execute("SELECT setting_value FROM plugin_settings WHERE setting_key = ?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _set_raw(self, key: str, value: str) -> None:
        self._ensure_table()
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            conn.execute(
                "INSERT INTO plugin_settings (setting_key, setting_value) VALUES (?, ?) ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def get_notify_groups(self) -> list[str]:
        raw = self._get_raw('notify_groups')
        if not raw:
            return []
        return [item for item in raw.split(',') if item]

    def set_notify_groups(self, groups: list[str]) -> None:
        self._set_raw('notify_groups', ','.join(sorted(set(groups))))

    def add_notify_group(self, group_id: str) -> list[str]:
        groups = self.get_notify_groups()
        if group_id not in groups:
            groups.append(group_id)
        self.set_notify_groups(groups)
        return groups

    def remove_notify_group(self, group_id: str) -> list[str]:
        groups = [item for item in self.get_notify_groups() if item != group_id]
        self.set_notify_groups(groups)
        return groups

    def get_watch_friends(self) -> list[str]:
        raw = self._get_raw('watch_friends')
        if not raw:
            return []
        return [item for item in raw.split(',') if item]

    def set_watch_friends(self, friend_ids: list[str]) -> None:
        self._set_raw('watch_friends', ','.join(sorted(set(friend_ids))))

    def add_watch_friend(self, friend_id: str) -> list[str]:
        items = self.get_watch_friends()
        if friend_id not in items:
            items.append(friend_id)
        self.set_watch_friends(items)
        return items

    def remove_watch_friend(self, friend_id: str) -> list[str]:
        items = [item for item in self.get_watch_friends() if item != friend_id]
        self.set_watch_friends(items)
        return items


class SearchRepository:
    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg

    def search_friends(self, keyword: str, limit: int = 10, offset: int = 0) -> tuple[int, list[FriendSnapshot]]:
        keyword = (keyword or '').strip()
        if not keyword:
            return 0, []
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            like_keyword = f"%{keyword}%"
            total_row = conn.execute(
                "SELECT COUNT(*) FROM friend_snapshots WHERE display_name LIKE ? OR friend_user_id LIKE ?",
                (like_keyword, like_keyword),
            ).fetchone()
            total = int(total_row[0]) if total_row else 0
            rows = conn.execute(
                "SELECT friend_user_id, display_name, status, location, status_description, updated_at FROM friend_snapshots WHERE display_name LIKE ? OR friend_user_id LIKE ? ORDER BY updated_at DESC, display_name ASC LIMIT ? OFFSET ?",
                (like_keyword, like_keyword, limit, offset),
            ).fetchall()
            items = [
                FriendSnapshot(friend_user_id=row[0], display_name=row[1], status=row[2], location=row[3], status_description=row[4], updated_at=row[5])
                for row in rows
            ]
            return total, items
        finally:
            conn.close()

    def count_cached_friends(self) -> int:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            row = conn.execute("SELECT COUNT(*) FROM friend_snapshots").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
