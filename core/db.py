import sqlite3

from .config import PluginConfig
from .models import FriendSnapshot, RadarEvent
from .utils import get_location_group_key


SNAPSHOT_SELECT_COLUMNS = (
    "friend_user_id, display_name, status, location, status_description, updated_at"
)
EVENT_SELECT_COLUMNS = (
    "eh.friend_user_id, COALESCE(fs.display_name, eh.friend_user_id), "
    "eh.event_type, eh.old_value, eh.new_value, eh.created_at"
)


class RadarDB:
    def __init__(self, cfg: PluginConfig):
        self.cfg = cfg

    def initialize(self) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS friend_snapshots (friend_user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, status TEXT, location TEXT, status_description TEXT, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS event_history (id INTEGER PRIMARY KEY AUTOINCREMENT, friend_user_id TEXT NOT NULL, event_type TEXT NOT NULL, old_value TEXT, new_value TEXT, created_at TEXT NOT NULL, dedupe_key TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS coroom_state (location_key TEXT PRIMARY KEY, signature TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self._migrate_friend_snapshots_table(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate_friend_snapshots_table(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(friend_snapshots)").fetchall()}
        if "status_description" not in columns:
            conn.execute("ALTER TABLE friend_snapshots ADD COLUMN status_description TEXT")

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

    @staticmethod
    def _event_from_row(row) -> RadarEvent:
        return RadarEvent(
            friend_user_id=row[0],
            display_name=row[1],
            event_type=row[2],
            old_value=row[3],
            new_value=row[4],
            created_at=row[5],
        )

    @staticmethod
    def _clean_ids(ids: list[str] | None) -> list[str]:
        return [str(item).strip() for item in (ids or []) if str(item).strip()]

    def upsert_friend_snapshots(self, snapshots: list[FriendSnapshot]) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            self._migrate_friend_snapshots_table(conn)
            conn.executemany(
                "INSERT INTO friend_snapshots (friend_user_id, display_name, status, location, status_description, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(friend_user_id) DO UPDATE SET display_name=excluded.display_name, status=excluded.status, location=excluded.location, status_description=excluded.status_description, updated_at=excluded.updated_at",
                [
                    (
                        item.friend_user_id,
                        item.display_name,
                        item.status,
                        item.location,
                        item.status_description,
                        item.updated_at,
                    )
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
                f"SELECT {SNAPSHOT_SELECT_COLUMNS} FROM friend_snapshots ORDER BY updated_at DESC, display_name ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._snapshot_from_row(row) for row in rows]
        finally:
            conn.close()

    def list_online_friend_snapshots(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            self._migrate_friend_snapshots_table(conn)
            rows = conn.execute(
                f"SELECT {SNAPSHOT_SELECT_COLUMNS} FROM friend_snapshots WHERE lower(COALESCE(status, '')) != 'offline' ORDER BY updated_at DESC, display_name ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._snapshot_from_row(row) for row in rows]
        finally:
            conn.close()

    def list_friend_snapshots_by_ids(self, friend_ids: list[str]) -> list[FriendSnapshot]:
        cleaned = self._clean_ids(friend_ids)
        if not cleaned:
            return []
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            self._migrate_friend_snapshots_table(conn)
            placeholders = ",".join(["?"] * len(cleaned))
            rows = conn.execute(
                f"SELECT {SNAPSHOT_SELECT_COLUMNS} FROM friend_snapshots WHERE friend_user_id IN ({placeholders}) ORDER BY display_name ASC",
                tuple(cleaned),
            ).fetchall()
            return [self._snapshot_from_row(row) for row in rows]
        finally:
            conn.close()

    def count_online_friend_snapshots(self) -> int:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM friend_snapshots WHERE lower(COALESCE(status, '')) != 'offline'"
            ).fetchone()
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
            rows = conn.execute(f"SELECT {SNAPSHOT_SELECT_COLUMNS} FROM friend_snapshots").fetchall()
            return {row[0]: self._snapshot_from_row(row) for row in rows}
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
                    (
                        event.friend_user_id,
                        event.event_type,
                        event.old_value,
                        event.new_value,
                        event.created_at,
                        f"{event.friend_user_id}:{event.event_type}:{event.old_value}:{event.new_value}",
                    )
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
                f"SELECT {EVENT_SELECT_COLUMNS} FROM event_history eh LEFT JOIN friend_snapshots fs ON eh.friend_user_id = fs.friend_user_id ORDER BY eh.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]
        finally:
            conn.close()

    def list_events_between(
        self,
        start_at: str,
        end_at: str,
        friend_ids: list[str] | None = None,
        limit: int = 5000,
    ) -> list[RadarEvent]:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            params: list = [start_at, end_at]
            where = ["eh.created_at >= ?", "eh.created_at <= ?"]
            cleaned = self._clean_ids(friend_ids)
            if cleaned:
                placeholders = ",".join(["?"] * len(cleaned))
                where.append(f"eh.friend_user_id IN ({placeholders})")
                params.extend(cleaned)
            params.append(max(1, int(limit)))
            sql = (
                f"SELECT {EVENT_SELECT_COLUMNS} "
                "FROM event_history eh "
                "LEFT JOIN friend_snapshots fs ON eh.friend_user_id = fs.friend_user_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY eh.id DESC LIMIT ?"
            )
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._event_from_row(row) for row in rows]
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

    def list_coroom_groups(
        self, friend_ids: list[str] | None = None, min_members: int = 2
    ) -> list[dict]:
        snapshots = self.list_online_friend_snapshots(limit=5000, offset=0)
        allow = set(friend_ids or [])
        grouped: dict[str, list[FriendSnapshot]] = {}
        for item in snapshots:
            if allow and item.friend_user_id not in allow:
                continue
            location_key = get_location_group_key(item.location)
            if not location_key:
                continue
            grouped.setdefault(location_key, []).append(item)

        result = []
        min_members = max(2, int(min_members))
        for location_key, members in grouped.items():
            if len(members) < min_members:
                continue
            members.sort(key=lambda x: x.display_name)
            result.append({"location_key": location_key, "members": members})
        result.sort(key=lambda x: (-len(x["members"]), x["location_key"]))
        return result

    def get_coroom_signature(self, location_key: str) -> str | None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT signature FROM coroom_state WHERE location_key = ?", (location_key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def set_coroom_signature(self, location_key: str, signature: str, updated_at: str) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            conn.execute(
                "INSERT INTO coroom_state (location_key, signature, updated_at) VALUES (?, ?, ?) ON CONFLICT(location_key) DO UPDATE SET signature=excluded.signature, updated_at=excluded.updated_at",
                (location_key, signature, updated_at),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_coroom_state_except(self, location_keys: list[str]) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            if not location_keys:
                conn.execute("DELETE FROM coroom_state")
            else:
                placeholders = ",".join(["?"] * len(location_keys))
                conn.execute(
                    f"DELETE FROM coroom_state WHERE location_key NOT IN ({placeholders})",
                    tuple(location_keys),
                )
            conn.commit()
        finally:
            conn.close()
