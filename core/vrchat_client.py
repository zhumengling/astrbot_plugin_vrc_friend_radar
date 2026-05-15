from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger

from .models import FriendSnapshot
from .vrchat_errors import (
    VRChatClientError,
    VRChatTwoFactorRequiredError,
    VRChatAuthInvalidError,
    VRChatNetworkError,
    VRChatRateLimitedError,
)
from .vrchat_auth import VRChatAuthMixin
from .vrchat_social import VRChatSocialMixin
from .vrchat_world import VRChatWorldMixin


@dataclass(slots=True)
class LoginResult:
    ok: bool
    user_id: str = ""
    display_name: str = ""
    message: str = ""


class VRChatClient(VRChatAuthMixin, VRChatSocialMixin, VRChatWorldMixin):
    def __init__(self, user_agent: str, request_timeout_seconds: int = 25, connect_timeout_seconds: int = 10):
        self.user_agent = user_agent
        self.request_timeout_seconds = max(5, int(request_timeout_seconds or 25))
        self.connect_timeout_seconds = max(3, min(self.request_timeout_seconds, int(connect_timeout_seconds or 10)))
        self._api_client = None
        self._configuration = None
        self._username = ""
        # VRChat 对 Boop 同目标有服务端冷却（通常 30-60 秒），这里做本地二次兜底，
        # 避免 LLM 一直重试触发 429。key=user_id, value=下一次允许 boop 的时间戳
        self._boop_next_allowed_ts: dict[str, float] = {}
        self._boop_min_interval_seconds = 45.0
        self._password = ""
        self._current_user_id = ""
        self._current_user_display_name = ""
        self._last_sync_debug: dict[str, int] = {}

    async def login(self, username: str, password: str, two_factor_code: str | None = None) -> LoginResult:
        return await asyncio.to_thread(self._login_sync, username, password, two_factor_code)

    @staticmethod
    def _build_exception_text(exc: Exception) -> str:
        parts = [str(exc or '')]
        reason = getattr(exc, 'reason', '')
        body = getattr(exc, 'body', '')
        if reason:
            parts.append(str(reason))
        if body:
            parts.append(str(body))
        return ' | '.join(p for p in parts if p).strip().lower()

    @staticmethod
    def _extract_status_code(exc: Exception) -> int | None:
        for attr in ('status', 'status_code', 'code', 'http_status'):
            value = getattr(exc, attr, None)
            try:
                number = int(value)
                if 100 <= number <= 599:
                    return number
            except Exception:
                continue

        text = VRChatClient._build_exception_text(exc)
        if not text:
            return None
        match = re.search(r'\b(401|403|429)\b', text)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return None
        return None

    @classmethod
    def _is_auth_invalid_exception(cls, exc: Exception) -> bool:
        status = cls._extract_status_code(exc)
        if status in (401, 403):
            return True
        text = cls._build_exception_text(exc)
        if not text:
            return False
        markers = (
            'unauthorized',
            'forbidden',
            'missing credentials',
            'missing authentication credentials',
            'authentication credentials were not provided',
            'invalid credentials',
            'invalid auth',
            'auth token',
            'jwt',
            'login required',
            'status 401',
            'status 403',
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _is_two_factor_challenge_exception(cls, exc: Exception) -> bool:
        text = cls._build_exception_text(exc)
        if not text:
            return False
        markers = (
            'email 2 factor authentication',
            '2 factor authentication',
            'two factor authentication',
            '2fa',
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _is_invalid_credentials_exception(cls, exc: Exception) -> bool:
        text = cls._build_exception_text(exc)
        if not text:
            return False
        markers = (
            'invalid username/email or password',
            'invalid username or password',
            'invalid email or password',
            'invalid username/email',
            'invalid credentials',
            'bad credentials',
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _is_network_exception(cls, exc: Exception) -> bool:
        text = cls._build_exception_text(exc)
        if not text:
            return False
        markers = (
            'timed out',
            'timeout',
            'connection reset',
            'connection aborted',
            'connection refused',
            'temporary failure in name resolution',
            'name or service not known',
            'network is unreachable',
            'failed to establish a new connection',
            'max retries exceeded',
            'ssl',
            'dns',
        )
        return any(marker in text for marker in markers)

    def is_auth_invalid_exception(self, exc: Exception) -> bool:
        return self._is_auth_invalid_exception(exc)

    @classmethod
    def _raise_as_client_error(
        cls,
        context: str,
        exc: Exception,
        *,
        invalid_credentials_in_login_phase: bool = False,
    ):
        status = cls._extract_status_code(exc)
        reason = str(getattr(exc, 'reason', '') or str(exc))
        if cls._is_two_factor_challenge_exception(exc):
            reason_lower = reason.lower()
            method = 'email' if 'email 2 factor authentication' in reason_lower else 'totp_or_recovery'
            raise VRChatTwoFactorRequiredError(method) from exc
        if cls._is_invalid_credentials_exception(exc):
            if invalid_credentials_in_login_phase:
                raise VRChatClientError(f"{context}: 用户名或密码错误") from exc
            # 运行期（非首次登录）出现 invalid credentials，更可能是会话/认证状态异常，而不是用户重新输错密码
            raise VRChatAuthInvalidError(
                f"{context}: 运行期认证状态异常(self/user unauthorized)，请重新登录",
                status=status,
                reason=reason,
            ) from exc
        if cls._is_auth_invalid_exception(exc):
            raise VRChatAuthInvalidError(f"{context}: 认证失效，请重新登录", status=status, reason=reason) from exc
        if cls._is_network_exception(exc):
            raise VRChatNetworkError(f"{context}: 网络异常或请求超时，请稍后重试") from exc
        raise VRChatClientError(f"{context}: {exc}") from exc

    @staticmethod
    def _to_text(value) -> str:
        return str(value or '').strip()

    @staticmethod
    def _is_web_platform(platform: str | None) -> bool:
        platform_text = str(platform or '').strip().lower()
        return platform_text == 'web'

    @staticmethod
    def _has_world_location(location: str | None) -> bool:
        text = str(location or '').strip().lower()
        return text.startswith('wrld_')

    def _extract_platform_info(self, user_obj) -> tuple[str, str]:
        # 返回 (platform, source)，source: presence/direct/history/last_platform
        # 仅 presence/direct 可视为"当前明确平台"；history/last_platform 仅作展示回退。
        presence = getattr(user_obj, 'presence', None)
        presence_platform = self._to_text(getattr(presence, 'platform', ''))
        if presence_platform:
            return presence_platform, 'presence'

        direct_platform = self._to_text(getattr(user_obj, 'platform', ''))
        if direct_platform:
            return direct_platform, 'direct'

        platform_history = getattr(user_obj, 'platform_history', None) or []
        for item in reversed(platform_history):
            platform = self._to_text(getattr(item, 'platform', ''))
            if platform:
                return platform, 'history'

        return self._to_text(getattr(user_obj, 'last_platform', '')), 'last_platform'

    def _extract_platform(self, user_obj) -> str:
        platform, _ = self._extract_platform_info(user_obj)
        return platform

    def _extract_status(self, user_obj) -> str:
        status = self._to_text(getattr(user_obj, 'status', ''))
        if status:
            return status
        presence = getattr(user_obj, 'presence', None)
        return self._to_text(getattr(presence, 'status', ''))

    def _extract_location(self, user_obj) -> str:
        location = self._to_text(getattr(user_obj, 'location', ''))
        if location:
            return location

        # CurrentUser 通常没有 top-level location，需要从 presence 组合。
        # 优先 traveling_*（过渡态），其次 world + instance（稳定态）。
        presence = getattr(user_obj, 'presence', None)
        traveling_to_world = self._to_text(getattr(presence, 'traveling_to_world', ''))
        traveling_to_instance = self._to_text(getattr(presence, 'traveling_to_instance', ''))
        if traveling_to_world:
            if traveling_to_instance:
                if ':' in traveling_to_instance:
                    return traveling_to_instance
                return f"{traveling_to_world}:{traveling_to_instance}"
            return traveling_to_world

        world = self._to_text(getattr(presence, 'world', ''))
        instance = self._to_text(getattr(presence, 'instance', ''))
        if world:
            if instance:
                if ':' in instance:
                    return instance
                return f"{world}:{instance}"
            return world

        return ''

    def _normalize_presence(
        self,
        status: str | None,
        location: str | None,
        platform: str | None,
        state: str | None = None,
        platform_source: str | None = None,
        is_self: bool = False,
    ) -> tuple[str, str]:
        status_text = self._to_text(status)
        location_text = self._to_text(location)
        platform_text = self._to_text(platform).lower()
        platform_source_text = self._to_text(platform_source).lower()
        state_text = self._to_text(state).lower()
        has_world_location = self._has_world_location(location_text)

        # 仅当"当前平台字段"明确为 web 时，才折叠为 Web 在线/离线。
        # 避免被 last_platform/platform_history 中的历史 web 误伤。
        explicit_web = self._is_web_platform(platform_text) and platform_source_text in {'presence', 'direct'}
        if explicit_web:
            if is_self:
                # 自我监控需要区分「Web 在线」与「客户端在线」。
                return 'offline', 'offline'
            return 'offline', 'offline'

        # 自我监控兜底：CurrentUser 里 state/status/location 可能短时不同步。
        # 当 status 显示在线，但没有有效世界位置信息时，优先判定为 Web 在线，
        # 避免和「真正在客户端内」混淆。
        if is_self:
            status_online = bool(status_text) and status_text.lower() != 'offline'
            if status_online and not has_world_location:
                if state_text in {'online', 'active', ''} or location_text.lower() in {'', 'offline'}:
                    return 'offline', 'offline'

        if state_text == 'offline':
            # self 场景下，state 偶发滞后时，以 status/location 的实时值优先，避免误判离线。
            if is_self and status_text and status_text.lower() != 'offline':
                if has_world_location:
                    return status_text, location_text
                return 'offline', 'offline'
            return 'offline', 'offline'

        return status_text, location_text

    def _build_snapshot_from_user(self, user_obj, now: str) -> FriendSnapshot:
        user_id = self._to_text(getattr(user_obj, 'id', ''))
        display_name = self._to_text(getattr(user_obj, 'display_name', '')) or user_id
        status = self._extract_status(user_obj)
        location = self._extract_location(user_obj)
        platform, platform_source = self._extract_platform_info(user_obj)
        state = self._to_text(getattr(user_obj, 'state', ''))
        status, location = self._normalize_presence(
            status,
            location,
            platform,
            state=state,
            platform_source=platform_source,
            is_self=True,
        )

        return FriendSnapshot(
            friend_user_id=user_id,
            display_name=display_name,
            status=status,
            location=location,
            status_description=self._to_text(getattr(user_obj, 'status_description', '')),
            updated_at=now,
        )

    async def fetch_friend_snapshots(self, friend_ids: list[str] | None = None) -> list[FriendSnapshot]:
        return await asyncio.to_thread(self._fetch_friend_snapshots_sync, friend_ids or [])

    def _fetch_friend_snapshots_sync(self, friend_ids: list[str]) -> list[FriendSnapshot]:
        if self._api_client is None:
            raise VRChatClientError("尚未登录，无法获取好友列表")
        try:
            from vrchatapi.api import friends_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc

        api = friends_api.FriendsApi(self._api_client)
        allow_filter = set(friend_ids)
        now = datetime.now().isoformat(timespec='seconds')
        dedup: dict[str, FriendSnapshot] = {}
        online_count = 0
        offline_count = 0
        web_filtered_count = 0

        for offline_flag in (False, True):
            offset = 0
            n = 100
            while True:
                try:
                    batch = api.get_friends(offset=offset, n=n, offline=offline_flag, _request_timeout=self._request_timeout_tuple())
                except Exception as exc:
                    phase = 'offline' if offline_flag else 'online'
                    self._raise_as_client_error(f"获取好友列表失败({phase}, offset={offset})", exc)
                if not batch:
                    break
                if offline_flag:
                    offline_count += len(batch)
                else:
                    online_count += len(batch)
                for friend in batch:
                    friend_id = str(getattr(friend, 'id', '') or '')
                    if not friend_id:
                        continue
                    if allow_filter and friend_id not in allow_filter:
                        continue
                    raw_status = self._extract_status(friend)
                    raw_location = self._extract_location(friend)
                    platform, platform_source = self._extract_platform_info(friend)
                    state = self._to_text(getattr(friend, 'state', ''))
                    status, location = self._normalize_presence(
                        raw_status,
                        raw_location,
                        platform,
                        state=state,
                        platform_source=platform_source,
                        is_self=False,
                    )
                    if status == 'offline' and raw_status.strip().lower() != 'offline':
                        web_filtered_count += 1
                    candidate = FriendSnapshot(
                        friend_user_id=friend_id,
                        display_name=self._to_text(getattr(friend, 'display_name', '')),
                        status=status,
                        location=location,
                        status_description=self._to_text(getattr(friend, 'status_description', '')),
                        updated_at=now,
                    )
                    existing = dedup.get(friend_id)
                    if existing is None:
                        dedup[friend_id] = candidate
                    else:
                        # 同一轮分页可能出现重复记录：优先保留"在线/有世界位置信息"的快照，
                        # 避免后续批次（尤其 offline=True）用离线态覆盖在线态导致误判。
                        existing_status = self._to_text(existing.status).lower()
                        candidate_status = self._to_text(candidate.status).lower()
                        existing_has_world = self._has_world_location(existing.location)
                        candidate_has_world = self._has_world_location(candidate.location)
                        should_replace = False
                        if existing_status == 'offline' and candidate_status != 'offline':
                            should_replace = True
                        elif existing_status != 'offline' and candidate_status == 'offline':
                            should_replace = False
                        elif not existing_has_world and candidate_has_world:
                            should_replace = True
                        elif existing_has_world and not candidate_has_world:
                            should_replace = False
                        else:
                            # 信息量接近时以后到达记录为准
                            should_replace = True
                        if should_replace:
                            dedup[friend_id] = candidate
                if len(batch) < n:
                    break
                offset += n

        self._last_sync_debug = {
            'online_batch_total': online_count,
            'offline_batch_total': offline_count,
            'merged_total': len(dedup),
            'filter_count': len(allow_filter),
            'web_filtered_total': web_filtered_count,
        }
        return list(dedup.values())

    def _refresh_current_user_profile_sync(self) -> bool:
        if self._api_client is None:
            return False
        try:
            from vrchatapi.api import authentication_api
            api = authentication_api.AuthenticationApi(self._api_client)
            current_user = api.get_current_user(_request_timeout=self._request_timeout_tuple())
            self._current_user_id = str(getattr(current_user, 'id', '') or '')
            self._current_user_display_name = str(getattr(current_user, 'display_name', '') or '')
            return bool(self._current_user_id)
        except Exception:
            return False

    async def fetch_self_snapshot(self) -> FriendSnapshot | None:
        return await asyncio.to_thread(self._fetch_self_snapshot_sync)

    def _fetch_self_snapshot_sync(self) -> FriendSnapshot | None:
        if self._api_client is None:
            return None
        try:
            from vrchatapi.api import authentication_api
        except ImportError:
            return None
        try:
            api = authentication_api.AuthenticationApi(self._api_client)
            current_user = api.get_current_user(_request_timeout=self._request_timeout_tuple())
            self._current_user_id = str(getattr(current_user, 'id', '') or '')
            self._current_user_display_name = str(getattr(current_user, 'display_name', '') or '')
            now = datetime.now().isoformat(timespec='seconds')
            return self._build_snapshot_from_user(current_user, now)
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("获取当前用户信息失败", exc)
            return None

    async def list_notifications(self, notification_type: str | None = None, hidden: bool = False, n: int = 60) -> list[dict]:
        return await asyncio.to_thread(self._list_notifications_sync, notification_type, hidden, n)

    def _list_notifications_sync(self, notification_type: str | None, hidden: bool, n: int) -> list[dict]:
        if self._api_client is None:
            raise VRChatClientError("尚未登录，无法拉取站内通知")
        try:
            from vrchatapi.api import notifications_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc
        try:
            api = notifications_api.NotificationsApi(self._api_client)
            kwargs = {
                'n': max(1, min(int(n or 60), 100)),
                'hidden': bool(hidden),
                '_request_timeout': self._request_timeout_tuple(),
            }
            if notification_type:
                kwargs['type'] = notification_type
            notifications = api.get_notifications(**kwargs)
        except Exception as exc:
            self._raise_as_client_error("获取站内通知失败", exc)
            return []

        result: list[dict] = []
        for item in notifications or []:
            result.append({
                'id': self._to_text(getattr(item, 'id', '')),
                'type': self._to_text(getattr(item, 'type', '')),
                'sender_user_id': self._to_text(getattr(item, 'sender_user_id', '')),
                'sender_username': self._to_text(getattr(item, 'sender_username', '')),
                'receiver_user_id': self._to_text(getattr(item, 'receiver_user_id', '')),
                'message': self._to_text(getattr(item, 'message', '')),
                'details': getattr(item, 'details', {}) or {},
                'created_at': self._to_text(getattr(item, 'created_at', '')),
                'seen': bool(getattr(item, 'seen', False)),
            })
        return result

    async def mark_notification_seen(self, notification_id: str) -> bool:
        return await asyncio.to_thread(self._mark_notification_seen_sync, notification_id)

    def _mark_notification_seen_sync(self, notification_id: str) -> bool:
        target = str(notification_id or '').strip()
        if not target or self._api_client is None:
            return False
        try:
            from vrchatapi.api import notifications_api
        except ImportError:
            return False
        try:
            api = notifications_api.NotificationsApi(self._api_client)
            # vrchatapi 有多个可能的方法名，按兼容顺序尝试
            for method_name in ('mark_notification_as_read', 'see_notification'):
                method = getattr(api, method_name, None)
                if callable(method):
                    method(target, _request_timeout=self._request_timeout_tuple())
                    return True
            return False
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("标记站内通知已读失败", exc)
            return False

    async def download_image_authenticated(self, url: str, save_path: str) -> str:
        return await asyncio.to_thread(self._download_image_authenticated_sync, url, save_path)

    def _download_image_authenticated_sync(self, url: str, save_path: str) -> str:
        if not url:
            raise VRChatClientError("缺少图片地址")
        if self._api_client is None:
            raise VRChatClientError("尚未登录，无法使用已登录会话下载图片")
        import urllib.request
        cookie = self._extract_cookie_header()
        headers = {
            'User-Agent': self.user_agent,
            'Referer': 'https://vrchat.com/',
        }
        if cookie:
            headers['Cookie'] = cookie
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            with open(save_path, 'wb') as f:
                f.write(data)
            return save_path
        except Exception as exc:
            raise VRChatClientError(f"下载认证图片失败: {exc}") from exc

    def get_current_user_id(self) -> str:
        if self._current_user_id:
            return self._current_user_id
        # 兜底：恢复登录/运行中状态下若内存字段丢失，尝试即时刷新
        self._refresh_current_user_profile_sync()
        return self._current_user_id

    def get_current_user_display_name(self) -> str:
        if self._current_user_display_name:
            return self._current_user_display_name
        self._refresh_current_user_profile_sync()
        return self._current_user_display_name

    def get_current_user_location(self) -> str:
        """最近一次 fetch_self_snapshot 成功后的 location 字段，用于 /vrc邀请 本人实例邀请。"""
        # 注意：客户端本身不缓存 self snapshot，这里仅承诺返回空串。插件层的 MonitorService 有缓存。
        return ""

    def get_last_sync_debug(self) -> dict[str, int]:
        return dict(self._last_sync_debug)
