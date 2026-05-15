"""日报/周报/统计命令 Mixin。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.message.components import Image, Plain
from astrbot.core.utils.io import save_temp_img
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from PIL import Image as PILImage, ImageDraw

from ..core.utils import extract_world_id, infer_joinability
from ..core.vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class ReportCommandsMixin:
    """日报/周报/统计命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    async def _collect_hot_world_stats_today(self: 'VRCFriendRadarPlugin', top_n: int | None = None, friend_ids: list[str] | None = None) -> list[dict]:
        stat_friend_ids = [fid for fid in (friend_ids or self._get_today_online_friend_ids()) if fid]
        if not stat_friend_ids:
            return []
        top_n = max(1, int(top_n or self.cfg.daily_report_top_n))
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        all_events = self.db.list_events_between(start, end, friend_ids=None, limit=20000)
        events: list = []
        stat_set = set(stat_friend_ids)
        for item in all_events:
            if item.event_type == 'co_room':
                member_ids = [fid for fid in (item.new_value or '').split('|') if fid]
                if any(fid in stat_set for fid in member_ids):
                    events.append(item)
                continue
            if item.friend_user_id in stat_set:
                events.append(item)

        snapshots = self.db.list_friend_snapshots_by_ids(stat_friend_ids)

        stats_map: dict[str, dict] = {}

        def touch(world_id: str, friend_ids_list: list[str] | None, location: str | None, count_inc: int = 1):
            if not world_id:
                return
            item = stats_map.setdefault(world_id, {
                'world_id': world_id,
                'count': 0,
                'friend_ids': set(),
                'joinability': Counter(),
                'sample_location': '',
            })
            item['count'] += max(1, count_inc)
            for fid in (friend_ids_list or []):
                if fid:
                    item['friend_ids'].add(fid)
            joinability = infer_joinability(location)
            item['joinability'][joinability] += 1
            if location and not item['sample_location']:
                item['sample_location'] = location

        for event in events:
            if event.event_type == 'location_changed':
                location = event.new_value
                world_id = extract_world_id(location)
                touch(world_id, [event.friend_user_id], location, count_inc=1)
            elif event.event_type == 'co_room':
                location = event.friend_user_id
                world_id = extract_world_id(location)
                member_ids = [fid for fid in (event.new_value or '').split('|') if fid and fid in stat_set]
                touch(world_id, member_ids, location, count_inc=max(1, len(member_ids)))

        for snapshot in snapshots:
            if (snapshot.status or '').strip().lower() == 'offline':
                continue
            world_id = extract_world_id(snapshot.location)
            touch(world_id, [snapshot.friend_user_id], snapshot.location, count_inc=1)

        items = list(stats_map.values())
        items.sort(key=lambda x: (-x['count'], -len(x['friend_ids']), x['world_id']))
        result = []
        for item in items[:top_n]:
            info = await self._get_world_info_with_cache(item['world_id'])
            name = info.get('name') or item['world_id']
            result.append({
                'world_id': item['world_id'],
                'world_name': name,
                'count': int(item['count']),
                'friend_count': len(item['friend_ids']),
                'joinability': item['joinability'],
                'sample_location': item['sample_location'],
                'world_info': info,
            })
        return result

    async def _build_daily_report_components(self: 'VRCFriendRadarPlugin') -> list:
        now = datetime.now()
        date_text = now.strftime('%Y-%m-%d')
        top_n = self.cfg.daily_report_top_n
        report_hint = "新增命令 @机器人 vrc灵魂画像 命令可以查看人物画像"

        lines = [f"📘 VRChat 监控日报（{date_text}）"]

        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        all_events = self.db.list_events_between(start, end, friend_ids=None, limit=30000)
        stat_friend_ids = self._get_today_online_friend_ids(events=all_events)
        stat_set = set(stat_friend_ids)

        if not stat_friend_ids:
            lines.append("今日暂无上线好友（基于本地事件与快照）。")
            lines.append("")
            lines.append(report_hint)
            return [Plain("\n".join(lines))]

        events: list = []
        for item in all_events:
            if item.event_type == 'co_room':
                member_ids = [fid for fid in (item.new_value or '').split('|') if fid]
                if any(fid in stat_set for fid in member_ids):
                    events.append(item)
                continue
            if item.friend_user_id in stat_set:
                events.append(item)

        type_counter = Counter(e.event_type for e in events)
        lines.append(
            "今日事件概览："
            f"上线 {type_counter.get('friend_online', 0)} | "
            f"下线 {type_counter.get('friend_offline', 0)} | "
            f"状态变更 {type_counter.get('status_changed', 0)} | "
            f"切换地图 {type_counter.get('location_changed', 0)} | "
            f"同房提醒 {type_counter.get('co_room', 0)}"
        )

        snapshot_map = self.db.get_friend_snapshot_map()
        active_counter = Counter(
            item.friend_user_id
            for item in events
            if item.event_type != 'co_room' and item.friend_user_id in stat_set
        )
        if active_counter:
            lines.append(f"今日活跃好友 Top {top_n}：")
            for idx, (friend_id, cnt) in enumerate(active_counter.most_common(top_n), start=1):
                snapshot = snapshot_map.get(friend_id)
                display = self._sanitize_display_name_for_output(snapshot.display_name if snapshot else '')
                shown_name = display or '未知好友'
                lines.append(f"{idx}. {shown_name}")
        else:
            lines.append("今日活跃好友 Top：暂无可用事件数据。")

        hot_worlds = await self._collect_hot_world_stats_today(top_n=top_n, friend_ids=stat_friend_ids)
        if hot_worlds:
            lines.append(f"今日热门世界 Top {top_n}：")
            for idx, item in enumerate(hot_worlds, start=1):
                overview = self._format_joinability_overview(item['joinability'])
                lines.append(
                    f"{idx}. {item['world_name']} | 热度 {item['count']} | 涉及好友 {item['friend_count']} | {overview}"
                )
        else:
            lines.append("今日热门世界 Top：暂无可用世界数据。")

        lines.append("")

        components = [Plain("\n".join(lines))]

        if hot_worlds:
            recommend = hot_worlds[0]
            info = recommend.get('world_info') or {}
            description = (info.get('description') or '').strip()
            description_zh, translated = await self._translate_non_zh_description(recommend['world_id'], description)
            if len(description_zh) > 220:
                description_zh = description_zh[:220] + '...'
            rec_lines = [
                f"🎯 今日推荐世界：{recommend['world_name']}",
                f"世界ID：{recommend['world_id']}",
                f"简介：{description_zh or '暂无简介'}",
            ]
            if translated:
                rec_lines.append("（注：该简介由世界原文自动翻译，基于 AstrBot AI 生成，可能存在少量语义偏差）")
            components.append(Plain("\n".join(rec_lines)))
            img_url = info.get('thumbnail_image_url') or info.get('image_url') or ''
            if img_url:
                local_img = await self._download_image_to_temp(img_url)
                if local_img:
                    try:
                        components.append(Image.fromFileSystem(local_img))
                    except Exception as exc:
                        logger.error(f"[vrc_friend_radar] 世界推荐图片发送失败: {exc}")
        components.append(Plain(report_hint))
        return components

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc生成日报")
    async def generate_daily_report(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc生成日报", "", 1).strip()
        if raw == "推送":
            sent = await self._send_daily_report_to_notify_groups(mark_sent=False)
            if sent <= 0:
                yield event.plain_result("日报推送失败：当前无可用通知群或发送异常。")
            else:
                yield event.plain_result(f"已向 {sent} 个通知群推送日报（手动推送不计入自动去重日期）。")
            return
        try:
            components = await self._build_daily_report_components()
        except VRChatClientError as exc:
            yield event.plain_result(f"生成日报失败：{exc}")
            return
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 生成日报异常: {exc}")
            yield event.plain_result("生成日报时发生异常，请稍后重试。")
            return
        yield event.chain_result(components)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc生成周报")
    async def weekly_report(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        now = datetime.now()
        start_dt = now - timedelta(days=7)
        start = start_dt.isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        events_all = self.db.list_events_between(start, end, friend_ids=None, limit=50000)

        watch_ids = set(self.monitor.get_monitor_watch_friend_ids())
        scoped = []
        for item in events_all:
            if item.event_type == 'co_room':
                member_ids = [fid for fid in (item.new_value or '').split('|') if fid]
                if any(fid in watch_ids for fid in member_ids):
                    scoped.append(item)
            elif item.friend_user_id in watch_ids:
                scoped.append(item)

        if not scoped:
            yield event.plain_result("最近 7 天没有足够的监控事件用于生成周报。")
            return

        type_counter = Counter(e.event_type for e in scoped)
        unique_days = len({str(e.created_at)[:10] for e in scoped})
        active_counter = Counter(
            item.friend_user_id for item in scoped if item.event_type != 'co_room'
        )
        snapshot_map = self.db.get_friend_snapshot_map()

        lines = [f"📊 VRChat 好友雷达周报 ({start_dt.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')})"]
        lines.append(
            f"事件总量：{len(scoped)} 条（覆盖 {unique_days} 天）"
            f" | 上线 {type_counter.get('friend_online', 0)}"
            f" | 下线 {type_counter.get('friend_offline', 0)}"
            f" | 切图 {type_counter.get('location_changed', 0)}"
            f" | 同房 {type_counter.get('co_room', 0)}"
        )
        lines.append("")
        lines.append(f"活跃监控好友 Top {min(5, len(active_counter))}：")
        for idx, (friend_id, cnt) in enumerate(active_counter.most_common(5), start=1):
            snapshot = snapshot_map.get(friend_id)
            display = self._sanitize_display_name_for_output(snapshot.display_name) if snapshot else friend_id
            lines.append(f"{idx}. {display} | 事件数 {cnt}")

        hot_worlds_week: Counter = Counter()
        for item in scoped:
            if item.event_type == 'location_changed':
                wid = extract_world_id(item.new_value)
                if wid:
                    hot_worlds_week[wid] += 1
            elif item.event_type == 'co_room':
                wid = extract_world_id(item.friend_user_id)
                if wid:
                    hot_worlds_week[wid] += 1
        if hot_worlds_week:
            lines.append("")
            lines.append(f"本周热门世界 Top {min(5, len(hot_worlds_week))}：")
            for idx, (world_id, cnt) in enumerate(hot_worlds_week.most_common(5), start=1):
                name = await self._get_world_name(world_id) or world_id
                lines.append(f"{idx}. {name} | 热度 {cnt}")

        yield event.plain_result("\n".join(lines))

    @filter.command("vrc热门世界")
    async def hot_worlds(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc热门世界", "", 1).strip()
        top_n = int(raw) if raw.isdigit() else self.cfg.daily_report_top_n
        top_n = max(1, min(20, top_n))
        try:
            items = await self._collect_hot_world_stats_today(top_n=top_n)
        except VRChatClientError as exc:
            yield event.plain_result(f"获取热门世界失败：{exc}")
            return
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 统计热门世界异常: {exc}")
            yield event.plain_result("获取热门世界时发生异常，请稍后重试。")
            return
        if not items:
            yield event.plain_result("今日暂无热门世界统计数据（今日暂无上线好友）。")
            return
        lines = [f"今日热门世界 Top {top_n}（当天上线好友）:"]
        for idx, item in enumerate(items, start=1):
            overview = self._format_joinability_overview(item['joinability'])
            lines.append(f"{idx}. {item['world_name']} | 热度 {item['count']} | 涉及好友 {item['friend_count']} | {overview}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc导出事件")
    async def export_events(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc导出事件", "", 1).strip()
        days = 7
        if raw.isdigit():
            days = max(1, min(30, int(raw)))
        now = datetime.now()
        start = (now - timedelta(days=days)).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        events_all = self.db.list_events_between(start, end, friend_ids=None, limit=50000)

        export_dir = self.cfg.data_dir / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = export_dir / f"events_{now.strftime('%Y%m%d_%H%M%S')}.csv"
        import csv
        try:
            with filename.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['created_at', 'event_type', 'friend_user_id', 'display_name', 'old_value', 'new_value'])
                for item in events_all:
                    writer.writerow([
                        item.created_at,
                        item.event_type,
                        item.friend_user_id,
                        item.display_name,
                        item.old_value or '',
                        item.new_value or '',
                    ])
        except Exception as exc:
            yield event.plain_result(f"导出失败：{exc}")
            return
        yield event.plain_result(
            f"已导出最近 {days} 天 {len(events_all)} 条事件至：{filename}"
        )

    @filter.command("vrc热力图")
    async def activity_heatmap(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc热力图", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc热力图 名字或usr_xxx（展示最近 30 天上线热力）")
            return
        try:
            friend_id, display_name = await self._resolve_profile_target_interactive(event, raw, "渲染活动热力图")
        except VRChatClientError as exc:
            yield event.plain_result(f"渲染热力图失败：{exc}")
            return
        try:
            image_path = await self._render_activity_heatmap(friend_id, display_name)
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 渲染热力图异常: {exc}")
            yield event.plain_result(f"渲染热力图异常：{exc}")
            return
        if not image_path:
            yield event.plain_result(f"{display_name} 近 30 天没有足够的上线事件用于生成热力图。")
            return
        yield event.image_result(image_path)

    async def _render_activity_heatmap(self: 'VRCFriendRadarPlugin', friend_id: str, display_name: str) -> str | None:
        """画一张 7x24 的活动热力图：近 30 天内该好友的上线事件分布。

        仅使用已落库事件，不触发额外网络请求。
        """
        now = datetime.now()
        start_dt = now - timedelta(days=30)
        start = start_dt.isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        events = self.db.list_events_for_friend_between(friend_id, start, end, limit=20000)
        if not events:
            return None

        # 统计矩阵：weekday (0=Mon) × hour (0-23)
        matrix = [[0 for _ in range(24)] for _ in range(7)]
        total_events = 0
        for item in events:
            if item.event_type not in {'friend_online', 'location_changed'}:
                continue
            ts_text = str(item.created_at or '').strip()
            try:
                dt = datetime.fromisoformat(ts_text)
            except Exception:
                continue
            matrix[dt.weekday()][dt.hour] += 1
            total_events += 1
        if total_events <= 0:
            return None

        max_count = max(max(row) for row in matrix) or 1
        # 用插件内已导入的 PIL 绘制
        cell_size = 32
        margin_left = 90
        margin_top = 80
        margin_right = 40
        margin_bottom = 60
        width = margin_left + cell_size * 24 + margin_right
        height = margin_top + cell_size * 7 + margin_bottom

        canvas = PILImage.new('RGB', (width, height), (32, 24, 40))
        draw = ImageDraw.Draw(canvas)
        title_font = self._get_card_font(22, bold=True)
        label_font = self._get_card_font(14)
        small_font = self._get_card_font(12)

        draw.text((margin_left, 18), f"{display_name} 近 30 天 VRChat 活动热力图", font=title_font, fill=(255, 235, 248))
        draw.text((margin_left, 48), f"总样本 {total_events} 次上线/切图，单元格越亮代表活跃度越高", font=small_font, fill=(230, 210, 225))

        weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        for row in range(7):
            y = margin_top + row * cell_size
            draw.text((margin_left - 60, y + cell_size // 2 - 8), weekdays[row], font=label_font, fill=(230, 210, 225))

        for hour in range(0, 24, 2):
            x = margin_left + hour * cell_size
            draw.text((x + 4, margin_top - 22), f"{hour:02d}", font=small_font, fill=(230, 210, 225))

        for row in range(7):
            for hour in range(24):
                count = matrix[row][hour]
                ratio = count / max_count if max_count else 0
                # 粉紫渐变配色，和灵魂画像卡风格一致
                r = int(80 + 175 * ratio)
                g = int(40 + 80 * ratio)
                b = int(90 + 130 * ratio)
                box = (
                    margin_left + hour * cell_size + 1,
                    margin_top + row * cell_size + 1,
                    margin_left + (hour + 1) * cell_size - 1,
                    margin_top + (row + 1) * cell_size - 1,
                )
                draw.rectangle(box, fill=(r, g, b), outline=(60, 38, 58))
                if count > 0:
                    draw.text((box[0] + 6, box[1] + 6), str(count), font=small_font, fill=(255, 245, 250))

        legend_y = height - margin_bottom + 24
        draw.text((margin_left, legend_y), f"峰值：单小时 {max_count} 次", font=small_font, fill=(230, 210, 225))
        return save_temp_img(canvas)
