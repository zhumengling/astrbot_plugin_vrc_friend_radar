"""VRChat 认证与会话管理 Mixin。

本模块包含 VRChat API 客户端的认证、会话恢复、会话健康检查等方法。
由 VRChatClient 通过多重继承使用，self 即为客户端实例。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from astrbot.api import logger

from .vrchat_errors import (
    VRChatClientError,
    VRChatTwoFactorRequiredError,
    VRChatAuthInvalidError,
)

if TYPE_CHECKING:
    pass


class VRChatAuthMixin:
    """VRChat 认证与会话管理 Mixin。"""

    def _request_timeout_tuple(self) -> tuple[int, int]:
        return (self.connect_timeout_seconds, self.request_timeout_seconds)

    def _create_api_client(self, username: str, password: str, cookie: str | None = None):
        import vrchatapi
        configuration = vrchatapi.Configuration(username=username, password=password)
        api_client = vrchatapi.ApiClient(configuration, cookie=cookie)
        api_client.user_agent = self.user_agent
        return configuration, api_client

    def _login_sync(self, username: str, password: str, two_factor_code: str | None = None):
        from .vrchat_client import LoginResult

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

    async def restore_session(self, username: str, password: str, cookie: str):
        import asyncio
        return await asyncio.to_thread(self._restore_session_sync, username, password, cookie)

    def _restore_session_sync(self, username: str, password: str, cookie: str):
        from .vrchat_client import LoginResult

        if self._api_client is not None:
            self.close()
        if not username or not cookie:
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
        if self._api_client is None or not self._username:
            return None
        cookie = self._extract_cookie_header()
        if not cookie:
            return None
        # 安全策略：不持久化明文密码，仅持久化 username + cookie
        return {'username': self._username, 'cookie': cookie}

    async def probe_auth_token(self) -> bool:
        """轻量健康检查，优先使用 /auth 的 verifyAuthToken 端点。

        比 get_current_user 轻量很多，允许把健康检查间隔缩短到 600s 而不增加风控风险。
        任何失败都会归一到 VRChatClientError 或其子类抛出。
        """
        import asyncio
        return await asyncio.to_thread(self._probe_auth_token_sync)

    def _probe_auth_token_sync(self) -> bool:
        if self._api_client is None:
            return False
        try:
            from vrchatapi.api import authentication_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc
        try:
            api = authentication_api.AuthenticationApi(self._api_client)
            # verifyAuthToken 是官方最轻的鉴权健康检查接口
            verify = getattr(api, 'verify_auth_token', None)
            if callable(verify):
                verify(_request_timeout=self._request_timeout_tuple())
                return True
            # 旧版本兼容：退回 get_current_user
            api.get_current_user(_request_timeout=self._request_timeout_tuple())
            return True
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("verifyAuthToken 会话健康检查失败", exc)
            raise VRChatClientError(f"verifyAuthToken 异常: {exc}") from exc

    async def probe_session_health(self) -> bool:
        import asyncio
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

    async def verify_session_ready(self, require_friends_api: bool = True) -> bool:
        import asyncio
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
                friends_api.FriendsApi(self._api_client).get_friends(offset=0, n=1, offline=False, _request_timeout=self._request_timeout_tuple())
            return True
        except Exception as exc:
            self._raise_as_client_error("登录后会话校验失败", exc)

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
