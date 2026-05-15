"""插件通用辅助方法 Mixin。

提供搜索会话管理、群组/私聊判断、显示名清理、世界信息缓存、
翻译、图片下载等通用辅助方法，由 VRCFriendRadarPlugin 继承使用。
"""
from __future__ import annotations

import asyncio
import math
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.agent.message import UserMessageSegment, TextPart

from .search_state import SearchSession
from .utils import extract_world_id, format_location, infer_joinability
from .vrchat_errors import VRChatClientError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


class PluginHelpersMixin:
    """插件通用辅助方法 Mixin，self 即为插件实例。"""

    def _build_session_key(self: 'VRCFriendRadarPlugin', event: 'AiocqhttpMessageEvent') -> str:
        sender_id = "unknown"
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            pass

        origin = None
        for attr in ("unified_msg_origin", "session_id", "message_type"):
            try:
                value = getattr(event, attr, None)
                if value:
                    origin = str(value)
                    break
            except Exception:
                continue

        if not origin:
            group_id = self._get_group_id(event)
            origin = f"group:{group_id}" if group_id else "private"

        return f"{origin}:{sender_id}"

    def _cleanup_search_sessions(self: 'VRCFriendRadarPlugin') -> None:
        expired = [key for key, session in self._search_sessions.items() if session.is_expired(self.cfg.search_result_ttl_seconds)]
        for key in expired:
            self._search_sessions.pop(key, None)

    def _save_search_session(self: 'VRCFriendRadarPlugin', session_key: str, items: list) -> None:
        self._cleanup_search_sessions()
        self._search_sessions[session_key] = SearchSession(session_key=session_key, items=items, created_at=time.time())

    def _get_search_session(self: 'VRCFriendRadarPlugin', session_key: str) -> SearchSession | None:
        self._cleanup_search_sessions()
        return self._search_sessions.get(session_key)

    def _get_group_id(self: 'VRCFriendRadarPlugin', event: 'AiocqhttpMessageEvent') -> str | None:
        # aiocqhttp 在不同事件类型下字段位置不完全一致，这里做兼容兜底。
        try:
            message_obj = getattr(event, "message_obj", None)
            group_id = getattr(message_obj, "group_id", None)
            if group_id:
                return str(group_id)
        except Exception:
            pass

        for attr in ("group_id",):
            try:
                value = getattr(event, attr, None)
                if value:
                    return str(value)
            except Exception:
                continue

        try:
            session_id = getattr(event, "session_id", None)
            if isinstance(session_id, str) and session_id.startswith("group_"):
                return session_id.split("_", 1)[1]
        except Exception:
            pass
        return None

    def _is_private_event(self: 'VRCFriendRadarPlugin', event: 'AiocqhttpMessageEvent') -> bool:
        return self._get_group_id(event) is None

    def _sanitize_display_name_for_output(self: 'VRCFriendRadarPlugin', name: str | None) -> str:
        text = str(name or '').strip()
        if not text:
            return '未知好友'
        text = re.sub(r"（\s*usr_[^）]+\s*）", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\(\s*usr_[^)]+\s*\)", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*/\s*usr_[A-Za-z0-9_-]+", "", text, flags=re.IGNORECASE)
        text = text.strip(' -|，,')
        if re.fullmatch(r"usr_[A-Za-z0-9_-]+", text, flags=re.IGNORECASE):
            return '未知好友'
        return text or '未知好友'

    async def _build_online_friend_list_message(self: 'VRCFriendRadarPlugin', page: int = 1) -> str:
        page = max(1, page)
        page_size = 20
        total_online = self.monitor.count_online_cached_friends()
        if total_online <= 0:
            return "当前缓存中没有在线好友，请先执行：/vrc同步好友"
        snapshots = self.monitor.list_online_cached_friends(limit=page_size, offset=(page - 1) * page_size)
        total_pages = max(1, math.ceil(total_online / page_size))
        lines = [f"当前在线好友总数 {total_online} 人，当前第 {page}/{total_pages} 页："]
        cache_hits = 0
        cache_misses = 0
        for idx, item in enumerate(snapshots, start=1):
            world_id = extract_world_id(item.location)
            if world_id:
                cached = self.world_cache.get(world_id)
                if cached and cached.get('name'):
                    cache_hits += 1
                else:
                    cache_misses += 1
            world_text = await self._format_world_display(item.location)
            shown_name = self._sanitize_display_name_for_output(item.display_name)
            lines.append(f"{idx}. {shown_name} | 状态: {item.status or 'unknown'} | 地图: {world_text} | {infer_joinability(item.location, status=item.status)}")
        logger.info(f"[vrc_friend_radar] 在线好友列表世界解析完成: total={len(snapshots)}, cache_hits={cache_hits}, cache_misses={cache_misses}")
        if page < total_pages:
            lines.append(f"下一页可用：/vrc在线好友 {page + 1}")
        return "\n".join(lines)

    def _cleanup_temp_world_logo_files(
        self: 'VRCFriendRadarPlugin',
        temp_dir: Path,
        *,
        max_keep: int = 120,
        expire_seconds: int = 7 * 24 * 3600,
    ) -> None:
        try:
            files = [
                item
                for item in temp_dir.glob('world_logo_*')
                if item.is_file()
            ]
        except Exception:
            return

        if not files:
            return

        now_ts = time.time()
        for item in files:
            try:
                if now_ts - item.stat().st_mtime > max(3600, int(expire_seconds)):
                    item.unlink(missing_ok=True)
            except Exception:
                continue

        try:
            remaining = [
                item
                for item in temp_dir.glob('world_logo_*')
                if item.is_file()
            ]
        except Exception:
            return

        if len(remaining) <= max(10, int(max_keep)):
            return

        remaining.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for item in remaining[max(10, int(max_keep)):]:
            try:
                item.unlink(missing_ok=True)
            except Exception:
                continue

    async def _download_image_to_temp(self: 'VRCFriendRadarPlugin', url: str) -> str | None:
        if not url:
            return None
        suffix = '.jpg'
        lowered = url.lower()
        if '.png' in lowered:
            suffix = '.png'
        elif '.webp' in lowered:
            suffix = '.webp'
        temp_dir = Path(tempfile.gettempdir()) / 'vrc_friend_radar'
        temp_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_temp_world_logo_files(temp_dir)
        file_path = temp_dir / f"world_logo_{uuid.uuid4().hex}{suffix}"
        try:
            result = await self.monitor.client.download_image_authenticated(url, str(file_path))
            self._cleanup_temp_world_logo_files(temp_dir)
            return result
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 认证下载地图Logo失败，尝试公开下载: {exc}")
            try:
                import urllib.request

                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                with open(file_path, 'wb') as f:
                    f.write(data)
                self._cleanup_temp_world_logo_files(temp_dir)
                return str(file_path)
            except Exception as public_exc:
                logger.error(f"[vrc_friend_radar] 下载地图Logo失败: {public_exc}")
                return None

    async def _download_generic_image_to_temp(self: 'VRCFriendRadarPlugin', url: str) -> str | None:
        """通用（不走 VRChat 认证）的图片下载，用于 B 站封面等。"""
        if not url:
            return None
        suffix = '.jpg'
        lowered = url.lower()
        if '.png' in lowered:
            suffix = '.png'
        elif '.webp' in lowered:
            suffix = '.webp'
        temp_dir = Path(tempfile.gettempdir()) / 'vrc_friend_radar'
        temp_dir.mkdir(parents=True, exist_ok=True)
        file_path = temp_dir / f"bili_cover_{uuid.uuid4().hex}{suffix}"
        try:
            import httpx
            from .bilibili_parser import BilibiliParser
            headers = {
                'User-Agent': BilibiliParser.DEFAULT_USER_AGENT,
                'Referer': 'https://www.bilibili.com',
            }
            async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return None
                file_path.write_bytes(resp.content)
            return str(file_path)
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar][bili] 下载封面失败: {exc}")
            return None

    async def _get_world_name(self: 'VRCFriendRadarPlugin', location: str | None) -> str:
        world_id = extract_world_id(location)
        if not world_id:
            return format_location(location)
        cached = self.world_cache.get(world_id)
        if cached and cached.get('name'):
            logger.info(f"[vrc_friend_radar] 世界缓存命中: {world_id}")
            return cached['name']
        logger.info(f"[vrc_friend_radar] 世界缓存未命中，开始拉取世界信息: {world_id}")
        try:
            info = await self.monitor.client.get_world_info(world_id)
        except VRChatClientError as exc:
            logger.warning(f"[vrc_friend_radar] 获取世界信息失败，将使用兜底名称: world={world_id} err={exc}")
            return '某个世界'
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 获取世界信息异常，将使用兜底名称: world={world_id} err={exc}")
            return '某个世界'
        if info and info.get('name'):
            self.world_cache.set(world_id, info)
            return info['name']
        return '某个世界'

    async def _format_world_display(self: 'VRCFriendRadarPlugin', location: str | None) -> str:
        world_name = await self._get_world_name(location)
        instance_text = format_location(location)
        if not location or not extract_world_id(location):
            return instance_text
        if instance_text and instance_text != world_name:
            return f"{world_name}（{instance_text}）"
        return world_name

    async def _get_world_info_with_cache(self: 'VRCFriendRadarPlugin', world_id: str) -> dict:
        if not world_id:
            return {}
        cached = self.world_cache.get(world_id)
        if cached:
            return cached
        if not self.monitor.client.is_logged_in():
            return {}
        try:
            info = await self.monitor.client.get_world_info(world_id)
        except VRChatClientError as exc:
            logger.warning(f"[vrc_friend_radar] 获取世界详情失败，跳过实时补全: world={world_id} err={exc}")
            return {}
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 获取世界详情异常，跳过实时补全: world={world_id} err={exc}")
            return {}
        if info:
            self.world_cache.set(world_id, info)
            return info
        return {}

    async def _translate_non_zh_description(self: 'VRCFriendRadarPlugin', world_id: str, text: str) -> tuple[str, bool]:
        source = (text or '').strip()
        if not source:
            return "暂无简介", False
        if self._is_text_mostly_chinese(source):
            return source, False

        cached = self.settings_repo.get_world_desc_translation(world_id, source)
        if cached:
            return cached, True

        lock = self._get_translation_lock(world_id)
        async with lock:
            cached = self.settings_repo.get_world_desc_translation(world_id, source)
            if cached:
                return cached, True
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo="vrc_friend_radar:daily_report:translator"
                )
                if not provider_id:
                    using_provider = self.context.get_using_provider()
                    provider_id = using_provider.meta().id if using_provider else ""
                if not provider_id:
                    raise RuntimeError("未找到可用的聊天模型提供商")

                prompt = """请将以下 VRChat 世界简介翻译成简体中文。
要求：
1) 仅输出中文翻译结果，不要解释；
2) 保留专有名词（如世界名、人名）可使用原文；
3) 如果原文过短或无法翻译，尽量给出自然中文表达。"""

                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    contexts=[UserMessageSegment(content=[TextPart(text=f"原文：\n{source}")])],
                    system_prompt="你是一个专业翻译助手。",
                    prompt=prompt,
                )
                translated = (llm_resp.completion_text or '').strip()
                if translated:
                    self.settings_repo.set_world_desc_translation(world_id, source, translated)
                    return translated, True
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 世界简介翻译失败(world={world_id}): {exc}")

            return f"{source}\n（原文）", False

    def _is_text_mostly_chinese(self: 'VRCFriendRadarPlugin', text: str) -> bool:
        if not text:
            return True
        cjk_chars = re.findall(r"[一-鿿]", text)
        ascii_alpha_chars = re.findall(r"[A-Za-z]", text)
        if len(cjk_chars) >= 8:
            return True
        if len(cjk_chars) == 0 and len(ascii_alpha_chars) >= 6:
            return False
        return len(cjk_chars) / max(1, len(text)) >= 0.25

    def _get_translation_lock(self: 'VRCFriendRadarPlugin', world_id: str) -> asyncio.Lock:
        key = (world_id or '').strip() or '__unknown_world__'
        lock = self._translation_lock_map.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._translation_lock_map[key] = lock
        return lock

    def _escape_html(self: 'VRCFriendRadarPlugin', value: str | None) -> str:
        import html as html_module
        return html_module.escape(str(value or '').strip())

    def _format_joinability_overview(self: 'VRCFriendRadarPlugin', stats) -> str:
        if not stats:
            return "未知"
        parts = []
        for key in ("可加入", "不可进入", "未知"):
            count = int(stats.get(key, 0))
            if count > 0:
                parts.append(f"{key}{count}")
        return (" / ".join(parts) if parts else "未知")

    def _get_today_online_friend_ids(self: 'VRCFriendRadarPlugin', events: list | None = None) -> list[str]:
        """获取"今天上线过"的好友ID集合（低请求、本地口径）。"""
        from datetime import datetime
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
        end = now.isoformat(timespec='seconds')
        event_list = events if events is not None else self.db.list_events_between(start, end, friend_ids=None, limit=50000)

        today_ids = {
            item.friend_user_id
            for item in event_list
            if item.event_type == 'friend_online' and (item.friend_user_id or '').strip()
        }

        snapshot_map = self.db.get_friend_snapshot_map()
        for fid, snap in snapshot_map.items():
            if (snap.status or '').strip().lower() == 'offline':
                continue
            updated_at = (snap.updated_at or '').strip()
            if not updated_at or updated_at < start or updated_at > end:
                continue
            today_ids.add(fid)

        return sorted(today_ids)

    def _remember_private_admin_sender(self: 'VRCFriendRadarPlugin', event: 'AiocqhttpMessageEvent') -> None:
        if not self._is_private_event(event):
            return
        try:
            sender_id = str(event.get_sender_id() or '').strip()
        except Exception:
            sender_id = ''
        if sender_id:
            self._last_private_admin_sender_id = sender_id

    def _resolve_admin_notice_targets(self: 'VRCFriendRadarPlugin') -> list[str]:
        """优先读取 AstrBot 全局 admins_id；为空时回退到最近一次私聊登录管理者。"""
        admin_ids: list[str] = []
        try:
            cfg = self.context.get_config()
            raw_admins = cfg.get('admins_id', []) if hasattr(cfg, 'get') else []
            if isinstance(raw_admins, (list, tuple)):
                admin_ids = [str(item or '').strip() for item in raw_admins]
            elif isinstance(raw_admins, str):
                admin_ids = [raw_admins.strip()]
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 读取 AstrBot admins_id 失败: {exc}")

        deduped: list[str] = []
        seen: set[str] = set()
        for item in admin_ids:
            if not item or item in seen:
                continue
            seen.add(item)
            deduped.append(item)

        if deduped:
            return deduped

        fallback = self._last_private_admin_sender_id.strip()
        if fallback:
            logger.warning('[vrc_friend_radar] admins_id 为空，已回退到最近私聊管理员ID进行登录告警投递。')
            return [fallback]
        return []

    def _track_background_task(self: 'VRCFriendRadarPlugin', task: asyncio.Task, label: str) -> None:
        from .vrchat_errors import VRChatTwoFactorRequiredError

        def _done(done_task: asyncio.Task) -> None:
            try:
                done_task.result()
            except asyncio.CancelledError:
                logger.info(f"[vrc_friend_radar] 后台任务已取消: {label}")
            except VRChatTwoFactorRequiredError as exc:
                logger.info(f"[vrc_friend_radar] 后台登录任务结束于额外验证阶段: {label} | method={exc.method}")
            except Exception as exc:
                logger.warning(f"[vrc_friend_radar] 后台任务异常结束: {label} | {exc}", exc_info=True)

        task.add_done_callback(_done)

    def _is_public_friend_request_allowed(self: 'VRCFriendRadarPlugin') -> bool:
        return self.settings_repo.get_allow_public_friend_request() if self.settings_repo else self.cfg.allow_public_friend_request

    def _set_public_friend_request_allowed(self: 'VRCFriendRadarPlugin', enabled: bool) -> None:
        self.settings_repo.set_allow_public_friend_request(enabled)

    async def _post_login_auto_sync_and_reply(self: 'VRCFriendRadarPlugin', event: 'AiocqhttpMessageEvent') -> list[str]:
        messages: list[str] = []
        try:
            await self.monitor.sync_friends()
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 登录后自动同步好友失败: {exc}")
            messages.append("登录后自动同步好友失败，请稍后手动执行 /vrc同步好友")
            return messages

        if not self._is_private_event(event):
            logger.info("[vrc_friend_radar] 非私聊上下文，登录后仅自动同步好友，不回传在线好友列表")
            return messages

        try:
            messages.append(await self._build_online_friend_list_message(page=1))
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 登录后自动获取在线好友失败: {exc}")
            messages.append("登录后自动获取在线好友失败，请稍后手动执行 /vrc在线好友")
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 登录后自动回传在线好友异常: {exc}")
            messages.append("登录后自动回传在线好友失败，请稍后手动执行 /vrc在线好友")
        return messages
