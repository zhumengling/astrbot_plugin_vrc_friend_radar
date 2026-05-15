"""监控服务同房检测 Mixin。

本模块包含 MonitorService 的同房检测逻辑，包括：
- 构建同房事件（基于好友快照的 location 分组）
- 按时间间隔过滤同房事件（节流）
- 查询当前同房分组

由 MonitorService 通过多重继承使用，self 即为监控服务实例。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

from .models import FriendSnapshot, RadarEvent
from .utils import get_location_group_key, infer_joinability

if TYPE_CHECKING:
    pass


class MonitorCoroomMixin:
    """监控服务同房检测 Mixin。"""

    def _build_coroom_events(self, snapshots: list[FriendSnapshot]) -> list[RadarEvent]:
        now = datetime.now().isoformat(timespec='seconds')
        allow = set(self.get_monitor_watch_friend_ids())
        if not allow:
            logger.info('[vrc_friend_radar] coroom skipped: empty watch list')
            return []

        current_self_id = (self.client.get_current_user_id() or '').strip() if self.cfg.watch_self else ''
        self_present_in_round = bool(current_self_id and any(item.friend_user_id == current_self_id for item in snapshots))
        if self.cfg.watch_self and current_self_id and not self_present_in_round:
            logger.warning('[vrc_friend_radar] coroom self missing in this round: self_id=%s, snapshots=%s', current_self_id, len(snapshots))

        grouped: dict[str, list[FriendSnapshot]] = {}
        for item in snapshots:
            if item.friend_user_id not in allow:
                continue
            status_text = (item.status or '').strip().lower()
            if status_text == 'offline':
                continue
            location_key = get_location_group_key(item.location)
            if not location_key:
                logger.info(
                    '[vrc_friend_radar] coroom skip snapshot: user=%s name=%s status=%s location=%s reason=location_not_groupable',
                    item.friend_user_id,
                    item.display_name,
                    item.status,
                    item.location,
                )
                continue
            grouped.setdefault(location_key, []).append(item)

        if grouped:
            for location_key in sorted(grouped.keys()):
                logger.info(
                    '[vrc_friend_radar] coroom grouped: key=%s members=%s ids=%s',
                    location_key,
                    len(grouped[location_key]),
                    '|'.join(sorted(i.friend_user_id for i in grouped[location_key])),
                )
        else:
            logger.info('[vrc_friend_radar] coroom grouped: no valid location groups in this round')

        events: list[RadarEvent] = []
        active_location_keys: list[str] = []
        min_members = self.cfg.coroom_notify_min_members
        joinable_only = self.cfg.coroom_notify_joinable_only

        for location_key, members in grouped.items():
            member_count = len(members)
            if member_count < min_members:
                extra_reason = ''
                if self.cfg.watch_self and (not current_self_id or not self_present_in_round):
                    extra_reason = ' (possible self missing)'
                logger.info(
                    '[vrc_friend_radar] coroom group filtered: key=%s members=%s reason=min_members(%s)%s',
                    location_key,
                    member_count,
                    min_members,
                    extra_reason,
                )
                continue

            joinability = infer_joinability(location_key)
            if joinable_only and joinability != '可加入':
                logger.info(
                    '[vrc_friend_radar] coroom group filtered: key=%s members=%s reason=joinable_only joinability=%s',
                    location_key,
                    member_count,
                    joinability,
                )
                continue

            members.sort(key=lambda x: x.friend_user_id)
            signature = '|'.join(item.friend_user_id for item in members)
            old_signature = self.db.get_coroom_signature(location_key)
            self.db.set_coroom_signature(location_key, signature, now)
            active_location_keys.append(location_key)
            if old_signature == signature:
                logger.info('[vrc_friend_radar] coroom group deduped by signature: key=%s signature=%s', location_key, signature)
                continue

            display_names = '、'.join(sorted(item.display_name for item in members))
            events.append(
                RadarEvent(
                    friend_user_id=location_key,
                    display_name=display_names,
                    event_type='co_room',
                    old_value=old_signature,
                    new_value=signature,
                    created_at=now,
                )
            )
            logger.info(
                '[vrc_friend_radar] coroom event built: key=%s members=%s old_signature=%s new_signature=%s',
                location_key,
                member_count,
                old_signature,
                signature,
            )

        self.db.delete_coroom_state_except(active_location_keys)
        return events

    def _filter_coroom_events_by_interval(self, events: list[RadarEvent]) -> list[RadarEvent]:
        now_ts = time.time()
        min_interval = self.cfg.coroom_notify_interval_seconds
        result: list[RadarEvent] = []
        active_keys = set()
        for event in events:
            location_key = event.friend_user_id
            active_keys.add(location_key)
            last_ts = self._last_coroom_notify_at.get(location_key, 0.0)
            elapsed = now_ts - last_ts
            if elapsed < min_interval:
                logger.info(
                    '[vrc_friend_radar] coroom group filtered: key=%s reason=throttle elapsed=%.1fs min_interval=%ss',
                    location_key,
                    elapsed,
                    min_interval,
                )
                continue
            self._last_coroom_notify_at[location_key] = now_ts
            result.append(event)
        stale_keys = [
            key
            for key, ts in self._last_coroom_notify_at.items()
            if key not in active_keys and (now_ts - ts) > min_interval
        ]
        for key in stale_keys:
            self._last_coroom_notify_at.pop(key, None)
        return result

    def list_coroom_groups(self, apply_query_filters: bool = True) -> list[dict]:
        watch_ids = self.get_monitor_watch_friend_ids()
        if not watch_ids:
            return []
        min_members = self.cfg.coroom_notify_min_members if apply_query_filters else 2
        return self.db.list_coroom_groups(watch_ids, min_members=min_members)
