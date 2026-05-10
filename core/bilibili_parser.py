"""Bilibili 视频本地直链解析。

参考自油猴脚本 "BiliBili本地解析(Miro)"：
- 用 /x/player/pagelist 取 cid
- 用 /x/player/playurl?platform=html5 取直链（html5 平台不强制登录 cookie）

支持的输入：
- 纯 BV 号：BV1xxxxxxxxx
- 纯 AV 号：av12345
- b23.tv 短链：https://b23.tv/xxxxxx（会解一次 302）
- 完整视频 URL：https://www.bilibili.com/video/BV.../?p=3
- 含 bvid= 查询参数的 URL

解析结果：
    {
        'bvid': 'BV...', 'aid': 12345, 'cid': 67890,
        'title': '...', 'duration_seconds': 123,
        'part_title': '...', 'page': 1, 'total_pages': 1,
        'quality': 116, 'accept_quality': [...],
        'video_url': '...', 'backup_urls': [...],
        'format': 'mp4', 'size_bytes': 12345,
        'cover': 'https://...jpg',
    }
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - test fallback
    import logging
    logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BV <-> AV 互转（来自 bilibili-API-collect 的社区算法，油猴脚本同款）
# ---------------------------------------------------------------------------
_XOR_CODE = 23442827791579
_MAX_AID = 1 << 51
_BASE = 58
_BV_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"


def av_to_bv(av: str | int) -> str:
    av_str = str(av).strip().lower()
    if av_str.startswith('av'):
        av_str = av_str[2:]
    try:
        aid = int(av_str)
    except ValueError as exc:
        raise ValueError(f"不是合法的 AV 号: {av}") from exc

    bytes_ = ['B', 'V', '1', '0', '0', '0', '0', '0', '0', '0', '0', '0']
    bv_index = len(bytes_) - 1
    tmp = (_MAX_AID | aid) ^ _XOR_CODE
    while tmp > 0:
        bytes_[bv_index] = _BV_ALPHABET[tmp % _BASE]
        tmp //= _BASE
        bv_index -= 1
    bytes_[3], bytes_[9] = bytes_[9], bytes_[3]
    bytes_[4], bytes_[7] = bytes_[7], bytes_[4]
    return ''.join(bytes_)


@dataclass(slots=True)
class BiliParseResult:
    bvid: str
    aid: int
    cid: int
    title: str
    part_title: str
    page: int
    total_pages: int
    duration_seconds: int
    quality: int
    accept_quality: list[int]
    video_url: str
    backup_urls: list[str]
    format: str
    size_bytes: int
    cover: str

    def to_dict(self) -> dict:
        return {
            'bvid': self.bvid,
            'aid': self.aid,
            'cid': self.cid,
            'title': self.title,
            'part_title': self.part_title,
            'page': self.page,
            'total_pages': self.total_pages,
            'duration_seconds': self.duration_seconds,
            'quality': self.quality,
            'accept_quality': self.accept_quality,
            'video_url': self.video_url,
            'backup_urls': self.backup_urls,
            'format': self.format,
            'size_bytes': self.size_bytes,
            'cover': self.cover,
        }


class BilibiliParseError(Exception):
    pass


class BilibiliParser:
    _BV_RE = re.compile(r"BV[0-9A-Za-z]+")
    _AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
    _PAGE_RE = re.compile(r"[?&]p=(\d+)")
    _BVID_QUERY_RE = re.compile(r"[?&]bvid=([0-9A-Za-z]+)")
    _B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, cookie: Optional[str] = None, user_agent: Optional[str] = None, timeout_seconds: float = 15.0):
        self.cookie = (cookie or '').strip()
        self.user_agent = user_agent or self.DEFAULT_USER_AGENT
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    # ----------------------------- 输入解析 -----------------------------
    async def extract_bvid_and_page(self, text: str) -> tuple[str, int]:
        """从输入里解析出 (bvid, page)。输入可以是 BV 号、AV 号、URL、b23.tv 短链。"""
        raw = str(text or '').strip()
        if not raw:
            raise BilibiliParseError("请提供 B 站视频 BV 号、av 号或链接")

        # 短链：先做一次 HEAD/GET 解重定向
        b23_match = self._B23_RE.search(raw)
        if b23_match:
            resolved = await self._resolve_b23(b23_match.group(0))
            if resolved:
                raw = resolved

        # BV 号优先
        bv_match = self._BV_RE.search(raw)
        if bv_match:
            bvid = bv_match.group(0)
        else:
            bvid_query = self._BVID_QUERY_RE.search(raw)
            if bvid_query:
                bvid = bvid_query.group(1)
            else:
                av_match = self._AV_RE.search(raw)
                if av_match:
                    bvid = av_to_bv(av_match.group(1))
                else:
                    raise BilibiliParseError("未在输入中找到有效的 BV 号、AV 号或视频链接")

        page_match = self._PAGE_RE.search(raw)
        page = int(page_match.group(1)) if page_match else 1
        if page < 1:
            page = 1
        return bvid, page

    async def _resolve_b23(self, short_url: str) -> str:
        headers = {'User-Agent': self.user_agent}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True, headers=headers) as client:
                resp = await client.get(short_url)
                # 无论 2xx/3xx 都能拿到最终 URL
                return str(resp.url)
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar][bili] 解析 b23.tv 短链失败: {exc}")
            return ''

    # ----------------------------- 两步解析 -----------------------------
    async def parse(self, text: str, quality: int = 116) -> BiliParseResult:
        bvid, page = await self.extract_bvid_and_page(text)
        return await self.parse_by_bvid(bvid, page=page, quality=quality)

    async def parse_by_bvid(self, bvid: str, page: int = 1, quality: int = 116) -> BiliParseResult:
        bvid = str(bvid or '').strip()
        if not bvid.startswith('BV'):
            raise BilibiliParseError(f"不是合法的 BV 号: {bvid}")
        page = max(1, int(page or 1))
        headers = self._build_headers(bvid=bvid)

        async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=headers, follow_redirects=True) as client:
            pagelist, video_meta = await self._fetch_pagelist_and_meta(client, bvid)
            if page > len(pagelist):
                raise BilibiliParseError(f"分 P 超出范围：该视频共 {len(pagelist)} 个分 P")
            part = pagelist[page - 1]
            cid = int(part.get('cid') or 0)
            part_title = str(part.get('part') or part.get('title') or '')
            duration = int(part.get('duration') or 0)
            if not cid:
                raise BilibiliParseError("未能取到 cid，视频可能已被删除或不可用")

            playurl = await self._fetch_playurl(client, bvid, cid, quality)

        durl = (playurl.get('durl') or [])
        if not durl:
            raise BilibiliParseError("未能取到视频直链（可能是大会员专属、付费或区域限制）")
        first = durl[0]
        video_url = str(first.get('url') or '')
        backup_urls = [str(u) for u in (first.get('backup_url') or []) if u]
        if not video_url and backup_urls:
            video_url = backup_urls[0]
        if not video_url:
            raise BilibiliParseError("未能取到视频直链")

        accept_quality = [int(q) for q in (playurl.get('accept_quality') or []) if str(q).isdigit()]
        return BiliParseResult(
            bvid=bvid,
            aid=int(video_meta.get('aid') or 0),
            cid=cid,
            title=str(video_meta.get('title') or ''),
            part_title=part_title,
            page=page,
            total_pages=len(pagelist),
            duration_seconds=duration or int(playurl.get('timelength', 0) // 1000) if playurl.get('timelength') else duration,
            quality=int(playurl.get('quality') or quality),
            accept_quality=accept_quality,
            video_url=video_url,
            backup_urls=backup_urls,
            format=str(playurl.get('format') or ''),
            size_bytes=int(first.get('size') or 0),
            cover=str(video_meta.get('pic') or ''),
        )

    # ----------------------------- 底层请求 -----------------------------
    def _build_headers(self, bvid: Optional[str] = None) -> dict:
        headers = {
            'User-Agent': self.user_agent,
            'Referer': f"https://www.bilibili.com/video/{bvid}" if bvid else 'https://www.bilibili.com',
            'Origin': 'https://www.bilibili.com',
        }
        if self.cookie:
            headers['Cookie'] = self.cookie
        return headers

    async def _fetch_pagelist_and_meta(self, client: httpx.AsyncClient, bvid: str) -> tuple[list[dict], dict]:
        # pagelist 只给分 P；再拉一次 view 拿标题、封面、aid
        pagelist_task = client.get(f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}")
        view_task = client.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        try:
            pagelist_resp, view_resp = await self._gather(pagelist_task, view_task)
        except Exception as exc:
            raise BilibiliParseError(f"无法连接 bilibili API：{exc}") from exc

        pagelist_data = self._extract_data(pagelist_resp, "pagelist")
        if not isinstance(pagelist_data, list) or not pagelist_data:
            raise BilibiliParseError("视频分 P 信息为空")

        try:
            view_json = view_resp.json()
            view_data = view_json.get('data') or {}
        except Exception:
            view_data = {}
        return pagelist_data, view_data

    async def _fetch_playurl(self, client: httpx.AsyncClient, bvid: str, cid: int, quality: int) -> dict:
        params = {
            'bvid': bvid,
            'cid': cid,
            'qn': quality,
            'type': '',
            'otype': 'json',
            'platform': 'html5',
            'high_quality': 1,
        }
        try:
            resp = await client.get('https://api.bilibili.com/x/player/playurl', params=params)
        except Exception as exc:
            raise BilibiliParseError(f"拉取视频直链失败：{exc}") from exc
        return self._extract_data(resp, "playurl")

    @staticmethod
    def _extract_data(resp: httpx.Response, label: str) -> dict | list:
        if resp.status_code != 200:
            raise BilibiliParseError(f"{label} 接口 HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise BilibiliParseError(f"{label} 接口返回无法解析：{exc}") from exc
        if not isinstance(payload, dict):
            raise BilibiliParseError(f"{label} 接口返回格式异常")
        if int(payload.get('code', 0)) != 0:
            message = payload.get('message') or payload.get('msg') or 'unknown'
            raise BilibiliParseError(f"{label} 接口错误 code={payload.get('code')} msg={message}")
        return payload.get('data') or {}

    @staticmethod
    async def _gather(*awaitables):
        import asyncio
        return await asyncio.gather(*awaitables)

    # ----------------------------- 工具方法 -----------------------------
    @staticmethod
    def format_duration(seconds: int) -> str:
        seconds = max(0, int(seconds or 0))
        if seconds == 0:
            return '未知'
        hh, rem = divmod(seconds, 3600)
        mm, ss = divmod(rem, 60)
        if hh > 0:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return f"{mm:02d}:{ss:02d}"

    @staticmethod
    def format_size(bytes_value: int) -> str:
        size = max(0, int(bytes_value or 0))
        if size == 0:
            return '未知'
        unit_list = ['B', 'KB', 'MB', 'GB']
        idx = 0
        value = float(size)
        while value >= 1024 and idx < len(unit_list) - 1:
            value /= 1024
            idx += 1
        return f"{value:.2f} {unit_list[idx]}"
