import sqlite3
from .config import PluginConfig
from .models import FriendSnapshot, RadarEvent


class RadarDB:
    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg

    def initialize(self) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS friend_snapshots (friend_user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, status TEXT, location TEXT, status_description TEXT, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS event_history (id INTEGER PRIMARY KEY AUTOINCREMENT, friend_user_id TEXT NOT NULL, event_type TEXT NOT NULL, old_value TEXT, new_value TEXT, created_at TEXT NOT NULL, dedupe_key TEXT)")
            self._migrate_friend_snapshots_table(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate_friend_snapshots_table(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(friend_snapshots)").fetchall()}
        if 'status_description' not in columns:
            conn.execute("ALTER TABLE friend_snapshots ADD COLUMN status_description TEXT")

    def upsert_friend_snapshots(self, snapshots: list[FriendSnapshot]) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            self._migrate_friend_snapshots_table(conn)
            conn.executemany(
                "INSERT INTO friend_snapshots (friend_user_id, display_name, status, location, status_description, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(friend_user_id) DO UPDATE SET display_name=excluded.display_name, status=excluded.status, location=excluded.location, status_description=excluded.status_description, updated_at=excluded.updated_at",
                [
                    (item.friend_user_id, item.display_name, item.status, item.location, item.status_description, item.updated_at)
                    for item in snapshots
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def list_friend_snapshots(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            self._migrate_friend_snapshots_table(conn)
            rows = conn.execute(
                "SELECT friend_user_id, display_name, status, location, status_description, updated_at FROM friend_snapshots ORDER BY updated_at DESC, display_name ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [
                FriendSnapshot(friend_user_id=row[0], display_name=row[1], status=row[2], location=row[3], status_description=row[4], updated_at=row[5])
                for row in rows
            ]
        finally:
            conn.close()


    def list_online_friend_snapshots(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            self._migrate_friend_snapshots_table(conn)
            rows = conn.execute(
                "SELECT friend_user_id, display_name, status, location, status_description, updated_at FROM friend_snapshots WHERE lower(COALESCE(status, '')) != 'offline' ORDER BY updated_at DESC, display_name ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [
                FriendSnapshot(friend_user_id=row[0], display_name=row[1], status=row[2], location=row[3], status_description=row[4], updated_at=row[5])
                for row in rows
            ]
        finally:
            conn.close()

    def count_online_friend_snapshots(self) -> int:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            row = conn.execute("SELECT COUNT(*) FROM friend_snapshots WHERE lower(COALESCE(status, '')) != 'offline'").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def count_friend_snapshots(self) -> int:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            row = conn.execute("SELECT COUNT(*) FROM friend_snapshots").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def get_friend_snapshot_map(self) -> dict[str, FriendSnapshot]:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            self._migrate_friend_snapshots_table(conn)
            rows = conn.execute("SELECT friend_user_id, display_name, status, location, status_description, updated_at FROM friend_snapshots").fetchall()
            return {
                row[0]: FriendSnapshot(friend_user_id=row[0], display_name=row[1], status=row[2], location=row[3], status_description=row[4], updated_at=row[5])
                for row in rows
            }
        finally:
            conn.close()

    def insert_event_history(self, events: list[RadarEvent]) -> None:
        if not events:
            return
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            conn.executemany(
                "INSERT INTO event_history (friend_user_id, event_type, old_value, new_value, created_at, dedupe_key) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (event.friend_user_id, event.event_type, event.old_value, event.new_value, event.created_at, f"{event.friend_user_id}:{event.event_type}:{event.old_value}:{event.new_value}")
                    for event in events
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def list_recent_events(self, limit: int = 20) -> list[RadarEvent]:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT friend_user_id, event_type, old_value, new_value, created_at FROM event_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                RadarEvent(friend_user_id=row[0], display_name=row[0], event_type=row[1], old_value=row[2], new_value=row[3], created_at=row[4])
                for row in rows
            ]
        finally:
            conn.close()

    def event_exists_since(self, dedupe_key: str, created_at_lower_bound: str) -> bool:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM event_history WHERE dedupe_key = ? AND created_at >= ? LIMIT 1",
                (dedupe_key, created_at_lower_bound),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
