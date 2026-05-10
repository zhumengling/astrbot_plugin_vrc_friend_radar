import sqlite3
from datetime import datetime, timedelta

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.cfg.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def initialize(self) -> None:
        conn = self._connect()
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
            # 监控分组：好友→tag 映射（支持多 tag，逗号分隔）
            conn.execute(
                "CREATE TABLE IF NOT EXISTS friend_tags (friend_user_id TEXT PRIMARY KEY, tags TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
            )
            # 分组→通知群路由规则
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tag_group_routes (tag TEXT NOT NULL, group_id TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (tag, group_id))"
            )
            # 状态签名关键词订阅：keyword + 订阅者 IM ID
            conn.execute(
                "CREATE TABLE IF NOT EXISTS signature_keyword_subscribers (keyword TEXT NOT NULL, subscriber_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (keyword, subscriber_id))"
            )
            # 群隐私开关：group_id → 是否隐藏 location
            conn.execute(
                "CREATE TABLE IF NOT EXISTS group_privacy (group_id TEXT PRIMARY KEY, hide_location INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)"
            )
            # 站内通知缓存：承载 friendRequest / invite / requestInvite 的短期状态
            conn.execute(
                "CREATE TABLE IF NOT EXISTS vrc_notifications ("
                "id TEXT PRIMARY KEY, "
                "type TEXT NOT NULL, "
                "sender_user_id TEXT, "
                "sender_username TEXT, "
                "message TEXT, "
                "details TEXT, "
                "created_at TEXT NOT NULL, "
                "fetched_at TEXT NOT NULL, "
                "consumed INTEGER NOT NULL DEFAULT 0"
                ")"
            )
            # 好友履历：首次发现日期、最近显示名、改名历史
            conn.execute(
                "CREATE TABLE IF NOT EXISTS friend_profiles ("
                "friend_user_id TEXT PRIMARY KEY, "
                "first_seen_at TEXT NOT NULL, "
                "last_display_name TEXT NOT NULL DEFAULT '', "
                "updated_at TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS friend_name_history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "friend_user_id TEXT NOT NULL, "
                "old_display_name TEXT NOT NULL, "
                "new_display_name TEXT NOT NULL, "
                "changed_at TEXT NOT NULL"
                ")"
            )
            # 本地好友备注（独立于 VRChat 官方 userNote，避免每次都打 API）
            conn.execute(
                "CREATE TABLE IF NOT EXISTS friend_notes ("
                "friend_user_id TEXT PRIMARY KEY, "
                "note_text TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
            self._migrate_friend_snapshots_table(conn)
            self._ensure_indexes(conn)
            conn.commit()
        finally:
            conn.close()

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_friend_snapshots_updated_at ON friend_snapshots(updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_friend_snapshots_status_updated ON friend_snapshots(status, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_history_created_at ON event_history(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_history_dedupe_created ON event_history(dedupe_key, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_history_friend_created ON event_history(friend_user_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tag_group_routes_tag ON tag_group_routes(tag)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vrc_notifications_type_fetched ON vrc_notifications(type, fetched_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signature_keyword_subscribers_keyword ON signature_keyword_subscribers(keyword)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_friend_name_history_friend_changed ON friend_name_history(friend_user_id, changed_at DESC)")

    def _migrate_friend_snapshots_table(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(friend_snapshots)").fetchall()}
        if "status_description" not in columns:
            conn.execute("ALTER TABLE friend_snapshots ADD COLUMN status_description TEXT")

    @staticmethod
    def _sanitize_limit_offset(limit: int, offset: int = 0, max_limit: int = 50000) -> tuple[int, int]:
        safe_limit = max(1, min(int(limit), max_limit))
        safe_offset = max(0, int(offset))
        return safe_limit, safe_offset

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

    @staticmethod
    def _history_retention_lower_bound(days: int = 30) -> str:
        return (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec='seconds')

    def _cleanup_old_history(self, conn: sqlite3.Connection, *, retention_days: int = 30) -> None:
        lower_bound = self._history_retention_lower_bound(retention_days)
        conn.execute(
            "DELETE FROM event_history WHERE created_at < ?",
            (lower_bound,),
        )
        conn.execute(
            "DELETE FROM coroom_state WHERE updated_at < ?",
            (lower_bound,),
        )

    def upsert_friend_snapshots(self, snapshots: list[FriendSnapshot]) -> None:
        if not snapshots:
            return
        conn = self._connect()
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
                    if str(item.friend_user_id or '').strip()
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def list_friend_snapshots(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        limit, offset = self._sanitize_limit_offset(limit, offset, max_limit=5000)
        conn = self._connect()
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
        limit, offset = self._sanitize_limit_offset(limit, offset, max_limit=5000)
        conn = self._connect()
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
        conn = self._connect()
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
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM friend_snapshots WHERE lower(COALESCE(status, '')) != 'offline'"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def count_friend_snapshots(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM friend_snapshots").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def get_friend_snapshot_map(self) -> dict[str, FriendSnapshot]:
        conn = self._connect()
        try:
            self._migrate_friend_snapshots_table(conn)
            rows = conn.execute(f"SELECT {SNAPSHOT_SELECT_COLUMNS} FROM friend_snapshots").fetchall()
            return {row[0]: self._snapshot_from_row(row) for row in rows}
        finally:
            conn.close()

    def insert_event_history(self, events: list[RadarEvent]) -> None:
        if not events:
            return
        conn = self._connect()
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
                    if str(event.friend_user_id or '').strip()
                ],
            )
            self._cleanup_old_history(conn, retention_days=30)
            conn.commit()
        finally:
            conn.close()

    def list_recent_events(self, limit: int = 20) -> list[RadarEvent]:
        limit, _ = self._sanitize_limit_offset(limit, 0, max_limit=5000)
        conn = self._connect()
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
        conn = self._connect()
        try:
            safe_limit, _ = self._sanitize_limit_offset(limit, 0, max_limit=100000)
            params: list = [start_at, end_at]
            where = ["eh.created_at >= ?", "eh.created_at <= ?"]
            cleaned = self._clean_ids(friend_ids)
            if cleaned:
                placeholders = ",".join(["?"] * len(cleaned))
                where.append(f"eh.friend_user_id IN ({placeholders})")
                params.extend(cleaned)
            params.append(safe_limit)
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

    def list_events_for_friend_between(
        self,
        friend_user_id: str,
        start_at: str,
        end_at: str,
        limit: int = 5000,
    ) -> list[RadarEvent]:
        target = str(friend_user_id or '').strip()
        if not target:
            return []
        return self.list_events_between(start_at, end_at, friend_ids=[target], limit=limit)

    def event_exists_since(self, dedupe_key: str, created_at_lower_bound: str) -> bool:
        conn = self._connect()
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
        allow = set(self._clean_ids(friend_ids))
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
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT signature FROM coroom_state WHERE location_key = ?", (location_key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def set_coroom_signature(self, location_key: str, signature: str, updated_at: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO coroom_state (location_key, signature, updated_at) VALUES (?, ?, ?) ON CONFLICT(location_key) DO UPDATE SET signature=excluded.signature, updated_at=excluded.updated_at",
                (location_key, signature, updated_at),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_coroom_state_except(self, location_keys: list[str]) -> None:
        conn = self._connect()
        try:
            cleaned = self._clean_ids(location_keys)
            if not cleaned:
                conn.execute("DELETE FROM coroom_state")
            else:
                placeholders = ",".join(["?"] * len(cleaned))
                conn.execute(
                    f"DELETE FROM coroom_state WHERE location_key NOT IN ({placeholders})",
                    tuple(cleaned),
                )
            conn.commit()
        finally:
            conn.close()


    # -----------------------------------------------------------------
    # 监控分组（tag）
    # -----------------------------------------------------------------
    @staticmethod
    def _normalize_tags(tags: list[str] | str | None) -> list[str]:
        if tags is None:
            return []
        if isinstance(tags, str):
            raw_items = [item.strip() for item in tags.replace('|', ',').replace('，', ',').split(',')]
        else:
            raw_items = [str(item).strip() for item in tags]
        seen: set[str] = set()
        result: list[str] = []
        for item in raw_items:
            if not item:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return sorted(result, key=str.casefold)

    def set_friend_tags(self, friend_user_id: str, tags: list[str]) -> list[str]:
        target = str(friend_user_id or '').strip()
        if not target:
            return []
        cleaned = self._normalize_tags(tags)
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            if cleaned:
                conn.execute(
                    "INSERT INTO friend_tags (friend_user_id, tags, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(friend_user_id) DO UPDATE SET tags=excluded.tags, updated_at=excluded.updated_at",
                    (target, ",".join(cleaned), now),
                )
            else:
                conn.execute("DELETE FROM friend_tags WHERE friend_user_id = ?", (target,))
            conn.commit()
            return cleaned
        finally:
            conn.close()

    def get_friend_tags(self, friend_user_id: str) -> list[str]:
        target = str(friend_user_id or '').strip()
        if not target:
            return []
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT tags FROM friend_tags WHERE friend_user_id = ?",
                (target,),
            ).fetchone()
            return self._normalize_tags(row[0]) if row else []
        finally:
            conn.close()

    def get_all_friend_tags(self) -> dict[str, list[str]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT friend_user_id, tags FROM friend_tags").fetchall()
            return {row[0]: self._normalize_tags(row[1]) for row in rows if row and row[0]}
        finally:
            conn.close()

    def add_tag_group_route(self, tag: str, group_id: str) -> None:
        tag_text = str(tag or '').strip()
        group_text = str(group_id or '').strip()
        if not tag_text or not group_text:
            return
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO tag_group_routes (tag, group_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tag, group_id) DO UPDATE SET updated_at=excluded.updated_at",
                (tag_text, group_text, now),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_tag_group_route(self, tag: str, group_id: str | None = None) -> int:
        tag_text = str(tag or '').strip()
        if not tag_text:
            return 0
        conn = self._connect()
        try:
            if group_id is None or str(group_id).strip() == '':
                cursor = conn.execute("DELETE FROM tag_group_routes WHERE tag = ?", (tag_text,))
            else:
                cursor = conn.execute(
                    "DELETE FROM tag_group_routes WHERE tag = ? AND group_id = ?",
                    (tag_text, str(group_id).strip()),
                )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()

    def get_tag_group_routes(self) -> dict[str, list[str]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT tag, group_id FROM tag_group_routes").fetchall()
            routes: dict[str, list[str]] = {}
            for tag, group_id in rows:
                routes.setdefault(str(tag or '').strip(), []).append(str(group_id or '').strip())
            # 去空、去重
            return {
                key: sorted({v for v in values if v})
                for key, values in routes.items()
                if key and values
            }
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # 签名关键词订阅
    # -----------------------------------------------------------------
    def add_signature_subscription(self, keyword: str, subscriber_id: str) -> None:
        key = str(keyword or '').strip()
        sub = str(subscriber_id or '').strip()
        if not key or not sub:
            return
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO signature_keyword_subscribers (keyword, subscriber_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(keyword, subscriber_id) DO NOTHING",
                (key, sub, now),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_signature_subscription(self, keyword: str, subscriber_id: str) -> int:
        key = str(keyword or '').strip()
        sub = str(subscriber_id or '').strip()
        if not key or not sub:
            return 0
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM signature_keyword_subscribers WHERE keyword = ? AND subscriber_id = ?",
                (key, sub),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()

    def list_signature_subscriptions(self, subscriber_id: str | None = None) -> list[tuple[str, str]]:
        conn = self._connect()
        try:
            if subscriber_id is None:
                rows = conn.execute(
                    "SELECT keyword, subscriber_id FROM signature_keyword_subscribers ORDER BY keyword ASC"
                ).fetchall()
            else:
                sub = str(subscriber_id or '').strip()
                if not sub:
                    return []
                rows = conn.execute(
                    "SELECT keyword, subscriber_id FROM signature_keyword_subscribers WHERE subscriber_id = ? ORDER BY keyword ASC",
                    (sub,),
                ).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # 群隐私
    # -----------------------------------------------------------------
    def set_group_privacy(self, group_id: str, hide_location: bool) -> None:
        gid = str(group_id or '').strip()
        if not gid:
            return
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO group_privacy (group_id, hide_location, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET hide_location=excluded.hide_location, updated_at=excluded.updated_at",
                (gid, 1 if hide_location else 0, now),
            )
            conn.commit()
        finally:
            conn.close()

    def get_hide_location_group_ids(self) -> set[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT group_id FROM group_privacy WHERE hide_location = 1"
            ).fetchall()
            return {str(row[0]) for row in rows if row and row[0]}
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # 站内通知缓存
    # -----------------------------------------------------------------
    def upsert_vrc_notifications(self, notifications: list[dict]) -> int:
        if not notifications:
            return 0
        import json
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            inserted = 0
            for item in notifications:
                notif_id = str(item.get('id') or '').strip()
                if not notif_id:
                    continue
                details = item.get('details') or {}
                try:
                    details_text = json.dumps(details, ensure_ascii=False)
                except Exception:
                    details_text = ''
                row = conn.execute(
                    "SELECT consumed FROM vrc_notifications WHERE id = ?",
                    (notif_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO vrc_notifications (id, type, sender_user_id, sender_username, message, details, created_at, fetched_at, consumed) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                        (
                            notif_id,
                            str(item.get('type') or ''),
                            str(item.get('sender_user_id') or ''),
                            str(item.get('sender_username') or ''),
                            str(item.get('message') or ''),
                            details_text,
                            str(item.get('created_at') or now),
                            now,
                        ),
                    )
                    inserted += 1
                else:
                    conn.execute(
                        "UPDATE vrc_notifications SET fetched_at = ? WHERE id = ?",
                        (now, notif_id),
                    )
            conn.commit()
            return inserted
        finally:
            conn.close()

    def list_vrc_notifications(self, notification_type: str | None = None, include_consumed: bool = False, limit: int = 50) -> list[dict]:
        import json
        safe_limit, _ = self._sanitize_limit_offset(limit, 0, max_limit=500)
        conn = self._connect()
        try:
            where = []
            params: list = []
            if notification_type:
                where.append("type = ?")
                params.append(str(notification_type))
            if not include_consumed:
                where.append("consumed = 0")
            sql = (
                "SELECT id, type, sender_user_id, sender_username, message, details, created_at, fetched_at, consumed "
                "FROM vrc_notifications"
            )
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY fetched_at DESC LIMIT ?"
            params.append(safe_limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
            result: list[dict] = []
            for row in rows:
                try:
                    details = json.loads(row[5]) if row[5] else {}
                except Exception:
                    details = {}
                result.append({
                    'id': row[0],
                    'type': row[1],
                    'sender_user_id': row[2],
                    'sender_username': row[3],
                    'message': row[4],
                    'details': details,
                    'created_at': row[6],
                    'fetched_at': row[7],
                    'consumed': bool(row[8]),
                })
            return result
        finally:
            conn.close()

    def mark_vrc_notification_consumed(self, notification_id: str) -> bool:
        target = str(notification_id or '').strip()
        if not target:
            return False
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE vrc_notifications SET consumed = 1 WHERE id = ?",
                (target,),
            )
            conn.commit()
            return int(cursor.rowcount or 0) > 0
        finally:
            conn.close()

    def purge_old_vrc_notifications(self, days: int = 14) -> int:
        lower_bound = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec='seconds')
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM vrc_notifications WHERE fetched_at < ?",
                (lower_bound,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()


    # -----------------------------------------------------------------
    # 好友履历：首见日期 + 显示名 + 改名历史
    # -----------------------------------------------------------------
    def ensure_friend_profile(self, friend_user_id: str, display_name: str) -> dict:
        """若不存在则创建 profile，返回 {first_seen_at, last_display_name}。

        不修改 last_display_name（那由 record_display_name_change 负责）。
        """
        target = str(friend_user_id or '').strip()
        if not target:
            return {}
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT first_seen_at, last_display_name, updated_at FROM friend_profiles WHERE friend_user_id = ?",
                (target,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO friend_profiles (friend_user_id, first_seen_at, last_display_name, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (target, now, str(display_name or ''), now),
                )
                conn.commit()
                return {'first_seen_at': now, 'last_display_name': str(display_name or ''), 'updated_at': now}
            return {
                'first_seen_at': row[0],
                'last_display_name': row[1] or '',
                'updated_at': row[2] or now,
            }
        finally:
            conn.close()

    def record_display_name_change(self, friend_user_id: str, old_display_name: str, new_display_name: str) -> None:
        target = str(friend_user_id or '').strip()
        old_name = str(old_display_name or '').strip()
        new_name = str(new_display_name or '').strip()
        if not target or not new_name or old_name == new_name:
            return
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO friend_name_history (friend_user_id, old_display_name, new_display_name, changed_at) "
                "VALUES (?, ?, ?, ?)",
                (target, old_name, new_name, now),
            )
            conn.execute(
                "INSERT INTO friend_profiles (friend_user_id, first_seen_at, last_display_name, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(friend_user_id) DO UPDATE SET last_display_name=excluded.last_display_name, updated_at=excluded.updated_at",
                (target, now, new_name, now),
            )
            conn.commit()
        finally:
            conn.close()

    def get_friend_profile(self, friend_user_id: str) -> dict | None:
        target = str(friend_user_id or '').strip()
        if not target:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT first_seen_at, last_display_name, updated_at FROM friend_profiles WHERE friend_user_id = ?",
                (target,),
            ).fetchone()
            if row is None:
                return None
            return {
                'friend_user_id': target,
                'first_seen_at': row[0],
                'last_display_name': row[1] or '',
                'updated_at': row[2] or '',
            }
        finally:
            conn.close()

    def list_friend_name_history(self, friend_user_id: str, limit: int = 20) -> list[dict]:
        target = str(friend_user_id or '').strip()
        if not target:
            return []
        safe_limit, _ = self._sanitize_limit_offset(limit, 0, max_limit=200)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT old_display_name, new_display_name, changed_at FROM friend_name_history "
                "WHERE friend_user_id = ? ORDER BY changed_at DESC LIMIT ?",
                (target, safe_limit),
            ).fetchall()
            return [
                {'old_display_name': r[0], 'new_display_name': r[1], 'changed_at': r[2]}
                for r in rows
            ]
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # 本地好友备注
    # -----------------------------------------------------------------
    def set_friend_note(self, friend_user_id: str, note_text: str) -> None:
        target = str(friend_user_id or '').strip()
        if not target:
            return
        text = str(note_text or '').strip()
        now = datetime.now().isoformat(timespec='seconds')
        conn = self._connect()
        try:
            if text:
                conn.execute(
                    "INSERT INTO friend_notes (friend_user_id, note_text, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(friend_user_id) DO UPDATE SET note_text=excluded.note_text, updated_at=excluded.updated_at",
                    (target, text, now),
                )
            else:
                conn.execute("DELETE FROM friend_notes WHERE friend_user_id = ?", (target,))
            conn.commit()
        finally:
            conn.close()

    def get_friend_note(self, friend_user_id: str) -> dict | None:
        target = str(friend_user_id or '').strip()
        if not target:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT note_text, updated_at FROM friend_notes WHERE friend_user_id = ?",
                (target,),
            ).fetchone()
            if row is None:
                return None
            return {'friend_user_id': target, 'note_text': row[0] or '', 'updated_at': row[1] or ''}
        finally:
            conn.close()

    def list_friend_notes(self) -> dict[str, str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT friend_user_id, note_text FROM friend_notes").fetchall()
            return {row[0]: row[1] or '' for row in rows if row and row[0]}
        finally:
            conn.close()
