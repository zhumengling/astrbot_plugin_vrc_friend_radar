"""图片渲染工具方法 Mixin。

提供卡片字体加载、文本测量与换行、圆角矩形绘制、封面图片加载与粘贴等
图片渲染相关的辅助方法，由 VRCFriendRadarPlugin 继承使用。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image as PILImage, ImageDraw, ImageFilter, ImageFont
from astrbot.api import logger

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class RenderingMixin:
    """图片渲染工具方法 Mixin，self 即为插件实例。"""

    def _get_card_font(self: 'VRCFriendRadarPlugin', size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        # 1) 优先使用随插件捆绑的字体（推荐放 assets/fonts/NotoSansSC-Regular.otf 与 Bold）
        bundled_root = self.cfg.plugin_dir / 'assets' / 'fonts'
        bundled_candidates: list[Path] = []
        if bold:
            bundled_candidates.extend([
                bundled_root / 'NotoSansSC-Bold.otf',
                bundled_root / 'NotoSansCJKsc-Bold.otf',
                bundled_root / 'SourceHanSansSC-Bold.otf',
            ])
        bundled_candidates.extend([
            bundled_root / 'NotoSansSC-Regular.otf',
            bundled_root / 'NotoSansCJKsc-Regular.otf',
            bundled_root / 'SourceHanSansSC-Regular.otf',
        ])
        for candidate in bundled_candidates:
            try:
                if candidate.exists():
                    return ImageFont.truetype(str(candidate), size)
            except Exception:
                continue

        # 2) 退化到宿主系统常见中文字体（Windows / macOS / Linux 都各覆盖一份）
        font_candidates: list[str] = []
        if bold:
            font_candidates.extend([
                "msyhbd.ttc",
                "simhei.ttf",
                "PingFang.ttc",
                "NotoSansCJK-Bold.ttc",
                "NotoSansSC-Bold.otf",
                "WenQuanYi Zen Hei.ttf",
                "Arial-Bold.ttf",
                "DejaVuSans-Bold.ttf",
            ])
        font_candidates.extend([
            "msyh.ttc",
            "simsun.ttc",
            "PingFang.ttc",
            "NotoSansCJK-Regular.ttc",
            "NotoSansSC-Regular.otf",
            "WenQuanYi Zen Hei.ttf",
            "Arial.ttf",
            "DejaVuSans.ttf",
        ])
        for font_name in font_candidates:
            try:
                return ImageFont.truetype(font_name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _measure_text(self: 'VRCFriendRadarPlugin', draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[int, int]:
        bbox = draw.textbbox((0, 0), text or " ", font=font)
        return int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])

    def _wrap_text(self: 'VRCFriendRadarPlugin', draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
        raw_lines = str(text or '').splitlines() or ['']
        wrapped: list[str] = []
        for raw in raw_lines:
            line = raw.strip()
            if not line:
                wrapped.append('')
                continue
            current = ''
            for ch in line:
                candidate = f"{current}{ch}"
                width, _ = self._measure_text(draw, candidate, font)
                if current and width > max_width:
                    wrapped.append(current)
                    current = ch
                else:
                    current = candidate
            if current:
                wrapped.append(current)
        return wrapped or ['']

    def _draw_wrapped_text(
        self: 'VRCFriendRadarPlugin',
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        fill: tuple[int, int, int],
        x: int,
        y: int,
        max_width: int,
        line_spacing: int,
    ) -> int:
        lines = self._wrap_text(draw, text, font, max_width)
        _, line_height = self._measure_text(draw, '测试', font)
        for line in lines:
            draw.text((x, y), line, font=font, fill=fill)
            y += line_height + line_spacing
        return y

    def _draw_round_rect(
        self: 'VRCFriendRadarPlugin',
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        radius: int,
        fill: tuple[int, int, int, int],
        outline: tuple[int, int, int, int] | None = None,
        width: int = 1,
    ) -> None:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    def _load_cover_image(self: 'VRCFriendRadarPlugin', path: str, size: tuple[int, int]) -> PILImage.Image | None:
        local_path = str(path or '').strip()
        if not local_path or not Path(local_path).exists():
            return None
        try:
            img = PILImage.open(local_path).convert('RGB')
            return img.resize(size, PILImage.Resampling.LANCZOS)
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 加载封面失败: {exc}")
            return None

    def _paste_card_cover(
        self: 'VRCFriendRadarPlugin',
        target: PILImage.Image,
        cover: PILImage.Image | None,
        box: tuple[int, int, int, int],
        *,
        radius: int = 28,
        opacity: int = 56,
        blur_radius: int = 6,
    ) -> None:
        if cover is None:
            return
        width = max(1, box[2] - box[0])
        height = max(1, box[3] - box[1])
        try:
            local = cover.resize((width, height), PILImage.Resampling.LANCZOS).convert('RGBA')
            if blur_radius > 0:
                local = local.filter(ImageFilter.GaussianBlur(blur_radius))
            alpha = PILImage.new('L', (width, height), color=max(0, min(255, opacity)))
            rounded_mask = PILImage.new('L', (width, height), 0)
            ImageDraw.Draw(rounded_mask).rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
            local.putalpha(PILImage.composite(alpha, PILImage.new('L', (width, height), 0), rounded_mask))
            target.alpha_composite(local, dest=(box[0], box[1]))
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 粘贴封面失败: {exc}")

    def _short_world_label(self: 'VRCFriendRadarPlugin', world_name: str) -> str:
        text = str(world_name or '').strip()
        if not text:
            return '??'
        simplified = re.sub(r'[\s\-_/|]+', '', text)
        simplified = re.sub(r'[()\[\]{}\u3010\u3011\u300c\u300d]', '', simplified)
        if re.search(r'[\u4e00-\u9fff]', simplified):
            chars = [ch for ch in simplified if re.search(r'[\u4e00-\u9fffA-Za-z0-9]', ch)]
            return ''.join(chars[:2]) or simplified[:2]
        compact = re.sub(r'[^A-Za-z0-9]', '', simplified)
        return (compact[:3] or simplified[:3]).upper()
