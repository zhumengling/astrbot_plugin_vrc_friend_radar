import sqlite3

from astrbot.api import logger

from .config import PluginConfig
from .models import FriendSnapshot


class SettingsRepository:
    SETTINGS_TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS plugin_settings "
        "(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)"
    )
    TRANSLATION_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS world_desc_translations (
            world_id TEXT NOT NULL,
            source_desc TEXT NOT NULL,
            translated_desc TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (world_id, source_desc)
        )
    """

    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        try:
            conn.execute(self.SETTINGS_TABLE_SQL)
            conn.execute(self.TRANSLATION_TABLE_SQL)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_world_desc_translations_updated_at ON world_desc_translations(updated_at DESC)")
            conn.commit()
            self._initialized = True
        finally:
            conn.close()

    def _ensure_table(self) -> None:
        if not self._initialized:
            self.initialize()

    @staticmethod
    def _parse_csv(raw: str | None) -> list[str]:
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item and item.strip()]

    @staticmethod
    def _dump_csv(items: list[str]) -> str:
        return ",".join(sorted(set(items)))
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

    @staticmethod
    def _merge_union(left: list[str] | None, right: list[str] | None) -> list[str]:
        return SettingsRepository._normalize_str_list([*(left or []), *(right or [])])


    def _get_raw(self, key: str) -> str | None:
        self._ensure_table()
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        try:
            row = conn.execute(
                "SELECT setting_value FROM plugin_settings WHERE setting_key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _set_raw(self, key: str, value: str) -> None:
        self._ensure_table()
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        try:
            conn.execute(
                "INSERT INTO plugin_settings (setting_key, setting_value) VALUES (?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def get_notify_groups(self) -> list[str]:
        return self._normalize_str_list(self._parse_csv(self._get_raw("notify_groups")))

    def set_notify_groups(self, groups: list[str]) -> None:
        self._set_raw("notify_groups", self._dump_csv(self._normalize_str_list(groups)))

    def add_notify_group(self, group_id: str) -> list[str]:
        groups = self._merge_union(self.get_notify_groups(), [group_id])
        self.set_notify_groups(groups)
        return groups

    def remove_notify_group(self, group_id: str) -> list[str]:
        target = str(group_id or '').strip()
        groups = [item for item in self.get_notify_groups() if item != target]
        self.set_notify_groups(groups)
        return groups

    def get_watch_friends(self) -> list[str]:
        return self._normalize_str_list(self._parse_csv(self._get_raw("watch_friends")))

    def set_watch_friends(self, friend_ids: list[str]) -> None:
        self._set_raw("watch_friends", self._dump_csv(self._normalize_str_list(friend_ids)))

    def add_watch_friend(self, friend_id: str) -> list[str]:
        items = self._merge_union(self.get_watch_friends(), [friend_id])
        self.set_watch_friends(items)
        return items

    def remove_watch_friend(self, friend_id: str) -> list[str]:
        target = str(friend_id or '').strip()
        items = [item for item in self.get_watch_friends() if item != target]
        self.set_watch_friends(items)
        return items

    def sync_notify_groups_with_config(self, config_groups: list[str] | None) -> list[str]:
        merged = self._merge_union(config_groups, self.get_notify_groups())
        self.set_notify_groups(merged)
        return merged

    def sync_watch_friends_with_config(self, config_friend_ids: list[str] | None) -> list[str]:
        merged = self._merge_union(config_friend_ids, self.get_watch_friends())
        self.set_watch_friends(merged)
        return merged

    def get_daily_report_last_sent_date(self) -> str:
        return self._get_raw("daily_report_last_sent_date") or ""

    def set_daily_report_last_sent_date(self, date_str: str) -> None:
        self._set_raw("daily_report_last_sent_date", (date_str or "").strip())

    def _cleanup_world_desc_translations(self, conn: sqlite3.Connection) -> None:
        max_entries = int(getattr(self.cfg, "world_translation_cache_max_entries", 500) or 0)
        if max_entries <= 0:
            return
        conn.execute(
            """
            DELETE FROM world_desc_translations
            WHERE rowid NOT IN (
                SELECT rowid
                FROM world_desc_translations
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
            )
            """,
            (max_entries,),
        )

    def get_world_desc_translation(self, world_id: str, source_desc: str) -> str | None:
        self._ensure_table()
        world_id = (world_id or "").strip()
        source_desc = (source_desc or "").strip()
        if not world_id or not source_desc:
            return None
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        try:
            row = conn.execute(
                "SELECT translated_desc FROM world_desc_translations WHERE world_id = ? AND source_desc = ?",
                (world_id, source_desc),
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()

    def set_world_desc_translation(
        self, world_id: str, source_desc: str, translated_desc: str
    ) -> None:
        self._ensure_table()
        world_id = (world_id or "").strip()
        source_desc = (source_desc or "").strip()
        translated_desc = (translated_desc or "").strip()
        if not world_id or not source_desc or not translated_desc:
            return
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        try:
            conn.execute(
                """
                INSERT INTO world_desc_translations (world_id, source_desc, translated_desc, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(world_id, source_desc)
                DO UPDATE SET translated_desc = excluded.translated_desc, updated_at = excluded.updated_at
                """,
                (world_id, source_desc, translated_desc),
            )
            conn.commit()
            try:
                self._cleanup_world_desc_translations(conn)
                conn.commit()
            except Exception as exc:
                logger.warning(
                    f"[vrc_friend_radar] world_desc_translations cleanup failed: {exc}"
                )
        finally:
            conn.close()


class SearchRepository:
    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg

    @staticmethod
    def _snapshot_from_row(row) -> FriendSnapshot:
        return FriendSnapshot(
            friend_user_id=row[0],
            display_name=row[1],
            status=row[2],
            location=row[3],
            status_description=row[4],
            updated_at=row[5],
        )

    def search_friends(
        self, keyword: str, limit: int = 10, offset: int = 0
    ) -> tuple[int, list[FriendSnapshot]]:
        keyword = (keyword or "").strip()
        if not keyword:
            return 0, []
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        try:
            like_keyword = f"%{keyword}%"
            total_row = conn.execute(
                "SELECT COUNT(*) FROM friend_snapshots WHERE display_name LIKE ? OR friend_user_id LIKE ?",
                (like_keyword, like_keyword),
            ).fetchone()
            total = int(total_row[0]) if total_row else 0
            safe_limit = max(1, min(int(limit), 200))
            safe_offset = max(0, int(offset))
            rows = conn.execute(
                "SELECT friend_user_id, display_name, status, location, status_description, updated_at "
                "FROM friend_snapshots WHERE display_name LIKE ? OR friend_user_id LIKE ? "
                "ORDER BY updated_at DESC, display_name ASC LIMIT ? OFFSET ?",
                (like_keyword, like_keyword, safe_limit, safe_offset),
            ).fetchall()
            return total, [self._snapshot_from_row(row) for row in rows]
        finally:
            conn.close()

    def count_cached_friends(self) -> int:
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        try:
            row = conn.execute("SELECT COUNT(*) FROM friend_snapshots").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
