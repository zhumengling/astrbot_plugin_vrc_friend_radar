"""B站视频解析命令 Mixin。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.bilibili_parser import BilibiliParser, BilibiliParseError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class BiliCommandsMixin:
    """B站视频解析命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    def _get_bili_parser(self: 'VRCFriendRadarPlugin') -> BilibiliParser:
        parser = getattr(self, '_bili_parser', None)
        if parser is None:
            cookie = ''
            try:
                cookie = str(self._read_config_value('bilibili_cookie', '')).strip()
            except Exception:
                cookie = ''
            parser = BilibiliParser(cookie=cookie or None)
            self._bili_parser = parser
        return parser

    def _read_config_value(self: 'VRCFriendRadarPlugin', key: str, default=None):
        """从 AstrBotConfig 读取单个键，不存在则返回 default。插件级别容错。"""
        cfg = self.cfg.raw_config
        try:
            if hasattr(cfg, 'get'):
                return cfg.get(key, default)
            return getattr(cfg, key, default)
        except Exception:
            return default

    @filter.command("bili解析")
    async def bili_parse_command(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("bili解析", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/bili解析 BV号 / av号 / 视频链接（支持 b23.tv 短链，末尾可带 ?p=分P）")
            return
        parser = self._get_bili_parser()
        try:
            result = await parser.parse(raw, quality=116)
        except BilibiliParseError as exc:
            yield event.plain_result(f"解析失败：{exc}")
            return
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar][bili] 解析异常: {exc}")
            yield event.plain_result(f"解析异常：{exc}")
            return

        lines = [f"🎬 {result.title or result.bvid}"]
        if result.total_pages > 1:
            lines.append(f"分 P：第 {result.page}/{result.total_pages} P{' · ' + result.part_title if result.part_title else ''}")
        elif result.part_title and result.part_title != result.title:
            lines.append(f"分 P 名：{result.part_title}")
        lines.append(f"BV：{result.bvid}" + (f" | AV：{result.aid}" if result.aid else ""))
        lines.append(f"时长：{BilibiliParser.format_duration(result.duration_seconds)}"
                     f" | 清晰度：{result.quality}"
                     f"（可选 {', '.join(map(str, result.accept_quality)) or '未知'}）")
        if result.size_bytes:
            lines.append(f"文件大小：{BilibiliParser.format_size(result.size_bytes)}")
        lines.append(f"格式：{result.format or '未知'}")
        lines.append("直链（有效期有限，建议尽快使用）：")
        lines.append(result.video_url)
        if result.backup_urls:
            lines.append("备用直链：")
            for idx, url in enumerate(result.backup_urls[:3], start=1):
                lines.append(f"{idx}. {url}")

        components: list = [Plain("\n".join(lines))]
        if result.cover:
            local_cover = await self._download_generic_image_to_temp(result.cover)
            if local_cover:
                try:
                    components.append(Image.fromFileSystem(local_cover))
                except Exception as exc:
                    logger.warning(f"[vrc_friend_radar][bili] 附加封面图失败: {exc}")
        yield event.chain_result(components)

    @filter.command("bili封面")
    async def bili_cover_command(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("bili封面", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/bili封面 BV号 / av号 / 视频链接")
            return
        parser = self._get_bili_parser()
        try:
            bvid, _page = await parser.extract_bvid_and_page(raw)
        except BilibiliParseError as exc:
            yield event.plain_result(f"解析失败：{exc}")
            return

        # 拿封面不需要走 playurl，只查 /x/web-interface/view
        headers = {
            'User-Agent': parser.user_agent,
            'Referer': f'https://www.bilibili.com/video/{bvid}',
        }
        try:
            async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as client:
                resp = await client.get(f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}')
                payload = resp.json()
        except Exception as exc:
            yield event.plain_result(f"获取封面失败：{exc}")
            return
        if int(payload.get('code', 0)) != 0:
            yield event.plain_result(f"获取封面失败：{payload.get('message') or payload.get('msg') or 'unknown'}")
            return
        data = payload.get('data') or {}
        title = str(data.get('title') or bvid)
        cover = str(data.get('pic') or '')
        if not cover:
            yield event.plain_result(f"未找到 {title} 的封面。")
            return
        components: list = [Plain(f"🖼️ {title}\nBV：{bvid}\n封面链接：{cover}")]
        local_cover = await self._download_generic_image_to_temp(cover)
        if local_cover:
            try:
                components.append(Image.fromFileSystem(local_cover))
            except Exception as exc:
                logger.warning(f"[vrc_friend_radar][bili] 附加封面图失败: {exc}")
        yield event.chain_result(components)
