from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger

from .models import FriendSnapshot


class VRChatClientError(Exception):
    pass


class VRChatTwoFactorRequiredError(VRChatClientError):
    def __init__(self, method: str):
        super().__init__(f"需要额外的二步验证方式: {method}")
        self.method = method


class VRChatAuthInvalidError(VRChatClientError):
    def __init__(self, message: str, *, status: int | None = None, reason: str = ''):
        super().__init__(message)
        self.status = status
        self.reason = reason


class VRChatNetworkError(VRChatClientError):
    pass


@dataclass(slots=True)
class LoginResult:
    ok: bool
    user_id: str = ""
    display_name: str = ""
    message: str = ""


class VRChatClient:
    def __init__(self, user_agent: str, request_timeout_seconds: int = 25, connect_timeout_seconds: int = 10):
        self.user_agent = user_agent
        self.request_timeout_seconds = max(5, int(request_timeout_seconds or 25))
        self.connect_timeout_seconds = max(3, min(self.request_timeout_seconds, int(connect_timeout_seconds or 10)))
        self._api_client = None
        self._configuration = None
        self._username = ""
        self._password = ""
        self._current_user_id = ""
        self._current_user_display_name = ""
        self._last_sync_debug: dict[str, int] = {}

    async def login(self, username: str, password: str, two_factor_code: str | None = None) -> LoginResult:
        return await asyncio.to_thread(self._login_sync, username, password, two_factor_code)

    def _request_timeout_tuple(self) -> tuple[int, int]:
        return (self.connect_timeout_seconds, self.request_timeout_seconds)

    def _create_api_client(self, username: str, password: str, cookie: str | None = None):
        import vrchatapi
        configuration = vrchatapi.Configuration(username=username, password=password)
        api_client = vrchatapi.ApiClient(configuration, cookie=cookie)
        api_client.user_agent = self.user_agent
        return configuration, api_client

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
        match = re.search(r'\b(401|403)\b', text)
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
        # 仅 presence/direct 可视为“当前明确平台”；history/last_platform 仅作展示回退。
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

        # 仅当“当前平台字段”明确为 web 时，才折叠为 Web 在线/离线。
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

    def _login_sync(self, username: str, password: str, two_factor_code: str | None = None) -> LoginResult:
        if self._api_client is not None:
            self.close()
        if not username or not password:
            raise VRChatClientError("缺少 VRChat 用户名或密码")
        try:
            from vrchatapi.api import authentication_api
            from vrchatapi.exceptions import UnauthorizedException
            from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
            from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖，请先安装 requirements.txt") from exc

        login_start = time.monotonic()
        logger.info(
            "[vrc_friend_radar] 开始执行 VRChat 登录请求 user=%s user_len=%s pwd_len=%s pwd_contains_ws=%s pwd_leading_ws=%s pwd_trailing_ws=%s UA=%s timeout=%s",
            username,
            len(username or ''),
            len(password or ''),
            any(ch.isspace() for ch in str(password or '')),
            bool(str(password or '') and str(password or '')[0].isspace()),
            bool(str(password or '') and str(password or '')[-1].isspace()),
            self.user_agent,
            self._request_timeout_tuple(),
        )

        for attempt in (1, 2):
            configuration, api_client = self._create_api_client(username, password)
            auth_api = authentication_api.AuthenticationApi(api_client)
            try:
                try:
                    logger.info(f"[vrc_friend_radar] 登录阶段 stage=initial_get_current_user start user={username} attempt={attempt}")
                    current_user = auth_api.get_current_user(_request_timeout=self._request_timeout_tuple())
                    logger.info(f"[vrc_friend_radar] 登录阶段 stage=initial_get_current_user ok user={username} attempt={attempt}")
                except UnauthorizedException as exc:
                    if exc.status != 200:
                        api_client.close()
                        self._raise_as_client_error("登录失败", exc, invalid_credentials_in_login_phase=True)
                    reason = str(getattr(exc, "reason", ""))
                    reason_lower = reason.lower()
                    full_text = self._build_exception_text(exc)
                    if "email 2 factor authentication" in reason_lower or "email 2 factor authentication" in full_text:
                        if not two_factor_code:
                            api_client.close()
                            raise VRChatTwoFactorRequiredError("email") from exc
                        logger.info(f"[vrc_friend_radar] 登录阶段 stage=verify_2fa_email start user={username} attempt={attempt}")
                        auth_api.verify2_fa_email_code(TwoFactorEmailCode(two_factor_code), _request_timeout=self._request_timeout_tuple())
                        logger.info(f"[vrc_friend_radar] 登录阶段 stage=verify_2fa_email ok user={username} attempt={attempt}")
                    elif (
                        "2 factor authentication" in reason_lower
                        or "two factor authentication" in full_text
                        or "2 factor authentication" in full_text
                        or "2fa" in full_text
                    ):
                        if not two_factor_code:
                            api_client.close()
                            raise VRChatTwoFactorRequiredError("totp_or_recovery") from exc
                        try:
                            logger.info(f"[vrc_friend_radar] 登录阶段 stage=verify_2fa_totp start user={username} attempt={attempt}")
                            auth_api.verify2_fa(TwoFactorAuthCode(two_factor_code), _request_timeout=self._request_timeout_tuple())
                            logger.info(f"[vrc_friend_radar] 登录阶段 stage=verify_2fa_totp ok user={username} attempt={attempt}")
                        except Exception:
                            logger.info(f"[vrc_friend_radar] 登录阶段 stage=verify_recovery_code start user={username} attempt={attempt}")
                            auth_api.verify_recovery_code(TwoFactorAuthCode(two_factor_code), _request_timeout=self._request_timeout_tuple())
                            logger.info(f"[vrc_friend_radar] 登录阶段 stage=verify_recovery_code ok user={username} attempt={attempt}")
                    elif self._is_invalid_credentials_exception(exc):
                        api_client.close()
                        raise VRChatClientError("登录失败: 用户名或密码错误") from exc
                    else:
                        api_client.close()
                        raise VRChatClientError(f"无法识别的登录挑战: {reason or exc}") from exc
                    logger.info(f"[vrc_friend_radar] 登录阶段 stage=post_2fa_get_current_user start user={username} attempt={attempt}")
                    current_user = auth_api.get_current_user(_request_timeout=self._request_timeout_tuple())
                    logger.info(f"[vrc_friend_radar] 登录阶段 stage=post_2fa_get_current_user ok user={username} attempt={attempt}")

                self._configuration = configuration
                self._api_client = api_client
                self._username = username
                self._password = password
                self._current_user_id = str(getattr(current_user, "id", "") or "")
                self._current_user_display_name = str(getattr(current_user, "display_name", "") or "")
                elapsed = time.monotonic() - login_start
                logger.info(f"[vrc_friend_radar] VRChat 登录成功 user={username} elapsed={elapsed:.2f}s attempt={attempt}")
                return LoginResult(ok=True, user_id=self._current_user_id, display_name=self._current_user_display_name, message="登录成功")
            except VRChatTwoFactorRequiredError:
                raise
            except VRChatClientError as exc:
                if attempt == 1 and (self._is_auth_invalid_exception(exc) or isinstance(exc, VRChatAuthInvalidError)):
                    logger.warning('[vrc_friend_radar] 登录阶段认证异常，执行 clearCookiesTryLogin 风格重试一次。err=%s', exc)
                    try:
                        rest_client = getattr(api_client, 'rest_client', None)
                        cookie_jar = getattr(rest_client, 'cookie_jar', None)
                        if cookie_jar is not None:
                            cookie_jar.clear()
                    except Exception:
                        pass
                    api_client.close()
                    self.close()
                    continue
                api_client.close()
                raise
            except Exception as exc:
                api_client.close()
                if attempt == 1 and self._is_auth_invalid_exception(exc):
                    logger.warning('[vrc_friend_radar] 登录阶段出现疑似认证污染，执行 clearCookiesTryLogin 风格重试一次。err=%s', exc)
                    self.close()
                    continue
                elapsed = time.monotonic() - login_start
                logger.error(f"[vrc_friend_radar] VRChat 登录异常 user={username} elapsed={elapsed:.2f}s error={exc}")
                self._raise_as_client_error("VRChat 登录异常", exc, invalid_credentials_in_login_phase=True)

        raise VRChatClientError("登录失败: 会话初始化异常")

    async def restore_session(self, username: str, password: str, cookie: str) -> LoginResult:
        return await asyncio.to_thread(self._restore_session_sync, username, password, cookie)

    def _restore_session_sync(self, username: str, password: str, cookie: str) -> LoginResult:
        if self._api_client is not None:
            self.close()
        if not username or not password or not cookie:
            raise VRChatClientError("缺少恢复登录态所需的信息")
        try:
            from vrchatapi.api import authentication_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖，请先安装 requirements.txt") from exc
        configuration, api_client = self._create_api_client(username, password, cookie=cookie)
        auth_api = authentication_api.AuthenticationApi(api_client)
        restore_start = time.monotonic()
        logger.info(f"[vrc_friend_radar] 开始恢复 VRChat 会话 user={username} timeout={self._request_timeout_tuple()}")
        try:
            current_user = auth_api.get_current_user(_request_timeout=self._request_timeout_tuple())
            self._configuration = configuration
            self._api_client = api_client
            self._username = username
            self._password = password
            self._current_user_id = str(getattr(current_user, "id", "") or "")
            self._current_user_display_name = str(getattr(current_user, "display_name", "") or "")
            elapsed = time.monotonic() - restore_start
            logger.info(f"[vrc_friend_radar] 恢复 VRChat 会话成功 user={username} elapsed={elapsed:.2f}s")
            return LoginResult(ok=True, user_id=self._current_user_id, display_name=self._current_user_display_name, message="恢复登录成功")
        except Exception as exc:
            elapsed = time.monotonic() - restore_start
            logger.error(f"[vrc_friend_radar] 恢复 VRChat 会话失败 user={username} elapsed={elapsed:.2f}s error={exc}")
            api_client.close()
            self._raise_as_client_error("恢复登录态失败", exc)

    def _extract_cookie_header(self) -> str:
        if self._api_client is None:
            return ''

        # 1) 优先读取 ApiClient.cookie（restore_session 场景通常可直接命中）
        cookie = str(getattr(self._api_client, 'cookie', '') or '').strip()
        if cookie:
            return cookie

        # 2) 兼容 vrchatapi-python 登录流程：cookie 常保存在 rest_client.cookie_jar
        try:
            rest_client = getattr(self._api_client, 'rest_client', None)
            cookie_jar = getattr(rest_client, 'cookie_jar', None)
            if cookie_jar is not None:
                pairs: list[str] = []
                for item in cookie_jar:
                    name = str(getattr(item, 'name', '') or '').strip()
                    if not name:
                        continue
                    value = str(getattr(item, 'value', '') or '')
                    pairs.append(f"{name}={value}")
                if pairs:
                    return '; '.join(pairs)
        except Exception:
            pass

        return ''

    def export_session(self) -> dict | None:
        if self._api_client is None or not self._username or not self._password:
            return None
        cookie = self._extract_cookie_header()
        if not cookie:
            return None
        return {'username': self._username, 'password': self._password, 'cookie': cookie}

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
                    batch = api.get_friends(offset=offset, n=n, offline=offline_flag)
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
                        # 同一轮分页可能出现重复记录：优先保留“在线/有世界位置信息”的快照，
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

    async def verify_session_ready(self, require_friends_api: bool = True) -> bool:
        return await asyncio.to_thread(self._verify_session_ready_sync, require_friends_api)

    def _verify_session_ready_sync(self, require_friends_api: bool = True) -> bool:
        if self._api_client is None:
            raise VRChatClientError("尚未登录，无法校验会话")
        try:
            from vrchatapi.api import authentication_api
            from vrchatapi.api import friends_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc

        try:
            auth_api = authentication_api.AuthenticationApi(self._api_client)
            current_user = auth_api.get_current_user(_request_timeout=self._request_timeout_tuple())
            self._current_user_id = str(getattr(current_user, 'id', '') or '')
            self._current_user_display_name = str(getattr(current_user, 'display_name', '') or '')
            if require_friends_api:
                friends_api.FriendsApi(self._api_client).get_friends(offset=0, n=1, offline=False)
            return True
        except Exception as exc:
            self._raise_as_client_error("登录后会话校验失败", exc)

    async def probe_session_health(self) -> bool:
        return await asyncio.to_thread(self._probe_session_health_sync)

    def _probe_session_health_sync(self) -> bool:
        if self._api_client is None:
            return False
        try:
            from vrchatapi.api import authentication_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc
        try:
            api = authentication_api.AuthenticationApi(self._api_client)
            current_user = api.get_current_user(_request_timeout=self._request_timeout_tuple())
            self._current_user_id = str(getattr(current_user, 'id', '') or '')
            self._current_user_display_name = str(getattr(current_user, 'display_name', '') or '')
            return True
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("会话健康检查失败", exc)
            raise VRChatClientError(f"会话健康检查异常: {exc}") from exc

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

    async def get_world_info(self, world_id: str) -> dict | None:
        return await asyncio.to_thread(self._get_world_info_sync, world_id)

    def _get_world_info_sync(self, world_id: str) -> dict | None:
        if not world_id or not world_id.startswith('wrld_') or self._api_client is None:
            return None
        try:
            from vrchatapi.api import worlds_api
        except ImportError:
            return None
        try:
            api = worlds_api.WorldsApi(self._api_client)
            world = api.get_world(world_id)
            return {
                'id': world_id,
                'name': str(getattr(world, 'name', '') or world_id),
                'description': str(getattr(world, 'description', '') or ''),
                'image_url': str(getattr(world, 'image_url', '') or ''),
                'thumbnail_image_url': str(getattr(world, 'thumbnail_image_url', '') or ''),
                'author_name': str(getattr(world, 'author_name', '') or ''),
                'capacity': int(getattr(world, 'capacity', 0) or 0),
            }
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("获取世界信息失败", exc)
            return None

    async def search_worlds(self, keyword: str, limit: int = 5, offset: int = 0) -> list[dict]:
        return await asyncio.to_thread(self._search_worlds_sync, keyword, limit, offset)

    def _search_worlds_sync(self, keyword: str, limit: int = 5, offset: int = 0) -> list[dict]:
        if not keyword or self._api_client is None:
            return []
        try:
            from vrchatapi.api import worlds_api
        except ImportError:
            return []
        try:
            api = worlds_api.WorldsApi(self._api_client)
            worlds = api.search_worlds(search=keyword, n=limit, offset=offset)
            return [
                {
                    'id': str(getattr(item, 'id', '') or ''),
                    'name': str(getattr(item, 'name', '') or ''),
                    'image_url': str(getattr(item, 'image_url', '') or ''),
                    'thumbnail_image_url': str(getattr(item, 'thumbnail_image_url', '') or ''),
                    'author_name': str(getattr(item, 'author_name', '') or ''),
                }
                for item in worlds
            ]
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("搜索世界失败", exc)
            return []


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

    def get_last_sync_debug(self) -> dict[str, int]:
        return dict(self._last_sync_debug)

    def is_logged_in(self) -> bool:
        return self._api_client is not None

    def get_saved_credentials(self) -> tuple[str, str]:
        return self._username, self._password

    def close(self) -> None:
        if self._api_client is not None:
            try:
                self._api_client.close()
            except Exception:
                pass
        self._api_client = None
        self._configuration = None
        self._username = ""
        self._password = ""
        self._current_user_id = ""
        self._current_user_display_name = ""
        self._last_sync_debug = {}
