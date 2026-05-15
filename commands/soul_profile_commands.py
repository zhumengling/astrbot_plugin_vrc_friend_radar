"""灵魂画像命令 Mixin。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class SoulProfileCommandsMixin:
    """灵魂画像命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    async def _build_public_soul_profile_image(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent, raw_target: str) -> str:
        friend_id, _ = self._resolve_profile_target(raw_target)
        summary = await self._build_soul_profile_summary(event, friend_id)
        return await self._render_soul_profile_card(summary)

    @filter.command("vrc灵魂画像")
    async def weekly_soul_profile(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc灵魂画像", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc灵魂画像 用户名字")
            return
        try:
            friend_id, _ = await self._resolve_profile_target_interactive(event, raw, "生成灵魂画像")
            image_url = await self._build_public_soul_profile_image(event, friend_id)
            yield event.image_result(image_url)
        except VRChatClientError as exc:
            yield event.plain_result(f"生成灵魂画像失败：{exc}")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 生成灵魂画像异常: {exc}")
            yield event.plain_result("生成灵魂画像时发生异常，请稍后重试。")

    @filter.command("vrc人设")
    async def persona_only(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc人设", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc人设 显示名")
            return
        try:
            friend_id, _ = await self._resolve_profile_target_interactive(event, raw, "生成 AI 人设")
            summary = await self._build_soul_profile_summary(event, friend_id)
            yield event.plain_result(summary.ai_persona_text)
        except VRChatClientError as exc:
            yield event.plain_result(f"生成人设失败：{exc}")

    @filter.command("命运指引")
    async def fortune_only(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("命运指引", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/命运指引 显示名")
            return
        try:
            friend_id, _ = await self._resolve_profile_target_interactive(event, raw, "生成命运指引")
            summary = await self._build_soul_profile_summary(event, friend_id)
            yield event.plain_result(summary.ai_fortune_text)
        except VRChatClientError as exc:
            yield event.plain_result(f"生成命运指引失败：{exc}")

    @filter.command("vrc缘分")
    async def relationship_score(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc缘分", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/vrc缘分 显示名")
            return
        try:
            target_id, target_name = await self._resolve_profile_target_interactive(event, raw, "寻找最有缘的人")
            now = datetime.now()
            days = max(1, int(self.cfg.soul_profile_days or 7))
            start_dt = now - timedelta(days=days)
            snapshot_map = self.db.get_friend_snapshot_map()
            partner_id, partner_name, overlap_minutes, overlap_worlds = self._estimate_companion_match(
                target_id,
                snapshot_map,
                start_dt,
                now,
            )
            if not partner_id or overlap_minutes <= 0:
                yield event.plain_result(f"{target_name} 最近 {days} 天还没有找到特别稳定的同游对象，像是在等一段更刚好的缘分慢慢靠近。")
                return

            score = min(99, max(36, overlap_worlds * 18 + overlap_minutes // 18))
            hours = overlap_minutes // 60
            minutes = overlap_minutes % 60
            duration_text = f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"
            yield event.plain_result(
                f"姻缘签：近 {days} 天里，{target_name} 命盘里最容易和 {partner_name} 相互照亮。\n"
                f"签文显示，你们在相近地图里累积相伴约 {duration_text}，重合世界 {overlap_worlds} 处，姻缘值约为 {score}/99。\n"
                f"这是一支\u201c慢热同心签\u201d，缘分不是一眼惊艳，而是次次同路后情绪悄悄落在同一个节拍里。"
                f"若这段缘线继续往前走，很容易从\u201c刚好同行\u201d慢慢长成\u201c互相惦记\u201d的柔软故事。"
            )
        except VRChatClientError as exc:
            yield event.plain_result(f"计算缘分指数失败：{exc}")
