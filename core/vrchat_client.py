from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from .models import FriendSnapshot


class VRChatClientError(Exception):
    pass


class VRChatTwoFactorRequiredError(VRChatClientError):
    def __init__(self, method: str):
        super().__init__(f"需要额外的二步验证方式: {method}")
        self.method = method


@dataclass(slots=True)
class LoginResult:
    ok: bool
    user_id: str = ""
    display_name: str = ""
    message: str = ""


class VRChatClient:
    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._api_client = None
        self._configuration = None
        self._username = ""
        self._password = ""
        self._last_sync_debug: dict[str, int] = {}

    async def login(self, username: str, password: str, two_factor_code: str | None = None) -> LoginResult:
        return await asyncio.to_thread(self._login_sync, username, password, two_factor_code)

    def _create_api_client(self, username: str, password: str, cookie: str | None = None):
        import vrchatapi
        configuration = vrchatapi.Configuration(username=username, password=password)
        api_client = vrchatapi.ApiClient(configuration, cookie=cookie)
        api_client.user_agent = self.user_agent
        return configuration, api_client

    def _login_sync(self, username: str, password: str, two_factor_code: str | None = None) -> LoginResult:
        if not username or not password:
            raise VRChatClientError("缺少 VRChat 用户名或密码")
        try:
            from vrchatapi.api import authentication_api
            from vrchatapi.exceptions import UnauthorizedException
            from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
            from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖，请先安装 requirements.txt") from exc

        configuration, api_client = self._create_api_client(username, password)
        auth_api = authentication_api.AuthenticationApi(api_client)

        try:
            try:
                current_user = auth_api.get_current_user()
            except UnauthorizedException as exc:
                if exc.status != 200:
                    api_client.close()
                    raise VRChatClientError(f"登录失败: {exc}") from exc
                reason = str(getattr(exc, "reason", ""))
                reason_lower = reason.lower()
                if "email 2 factor authentication" in reason_lower:
                    if not two_factor_code:
                        api_client.close()
                        raise VRChatTwoFactorRequiredError("email") from exc
                    auth_api.verify2_fa_email_code(TwoFactorEmailCode(two_factor_code))
                elif "2 factor authentication" in reason_lower:
                    if not two_factor_code:
                        api_client.close()
                        raise VRChatTwoFactorRequiredError("totp_or_recovery") from exc
                    try:
                        auth_api.verify2_fa(TwoFactorAuthCode(two_factor_code))
                    except Exception:
                        auth_api.verify_recovery_code(TwoFactorAuthCode(two_factor_code))
                else:
                    api_client.close()
                    raise VRChatClientError(f"无法识别的登录挑战: {reason}") from exc
                current_user = auth_api.get_current_user()

            self._configuration = configuration
            self._api_client = api_client
            self._username = username
            self._password = password
            return LoginResult(ok=True, user_id=str(getattr(current_user, "id", "")), display_name=str(getattr(current_user, "display_name", "")), message="登录成功")
        except VRChatClientError:
            raise
        except Exception as exc:
            api_client.close()
            raise VRChatClientError(f"VRChat 登录异常: {exc}") from exc

    async def restore_session(self, username: str, password: str, cookie: str) -> LoginResult:
        return await asyncio.to_thread(self._restore_session_sync, username, password, cookie)

    def _restore_session_sync(self, username: str, password: str, cookie: str) -> LoginResult:
        if not username or not password or not cookie:
            raise VRChatClientError("缺少恢复登录态所需的信息")
        try:
            from vrchatapi.api import authentication_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖，请先安装 requirements.txt") from exc
        configuration, api_client = self._create_api_client(username, password, cookie=cookie)
        auth_api = authentication_api.AuthenticationApi(api_client)
        try:
            current_user = auth_api.get_current_user()
            self._configuration = configuration
            self._api_client = api_client
            self._username = username
            self._password = password
            return LoginResult(ok=True, user_id=str(getattr(current_user, "id", "")), display_name=str(getattr(current_user, "display_name", "")), message="恢复登录成功")
        except Exception as exc:
            api_client.close()
            raise VRChatClientError(f"恢复登录态失败: {exc}") from exc

    def export_session(self) -> dict | None:
        if self._api_client is None or not self._username or not self._password:
            return None
        cookie = getattr(self._api_client, 'cookie', '') or ''
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

        for offline_flag in (False, True):
            offset = 0
            n = 100
            while True:
                batch = api.get_friends(offset=offset, n=n, offline=offline_flag)
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
                    dedup[friend_id] = FriendSnapshot(
                        friend_user_id=friend_id,
                        display_name=str(getattr(friend, 'display_name', '') or ''),
                        status=str(getattr(friend, 'status', '') or ''),
                        location=str(getattr(friend, 'location', '') or ''),
                        status_description=str(getattr(friend, 'status_description', '') or ''),
                        updated_at=now,
                    )
                if len(batch) < n:
                    break
                offset += n

        self._last_sync_debug = {'online_batch_total': online_count, 'offline_batch_total': offline_count, 'merged_total': len(dedup), 'filter_count': len(allow_filter)}
        return list(dedup.values())

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
                'image_url': str(getattr(world, 'image_url', '') or ''),
                'thumbnail_image_url': str(getattr(world, 'thumbnail_image_url', '') or ''),
                'author_name': str(getattr(world, 'author_name', '') or ''),
            }
        except Exception:
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
        except Exception:
            return []


    async def download_image_authenticated(self, url: str, save_path: str) -> str:
        return await asyncio.to_thread(self._download_image_authenticated_sync, url, save_path)

    def _download_image_authenticated_sync(self, url: str, save_path: str) -> str:
        if not url:
            raise VRChatClientError("缺少图片地址")
        if self._api_client is None:
            raise VRChatClientError("尚未登录，无法使用已登录会话下载图片")
        import urllib.request
        cookie = getattr(self._api_client, 'cookie', '') or ''
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

    def close(self) -> None:
        if self._api_client is not None:
            try:
                self._api_client.close()
            except Exception:
                pass
            self._api_client = None
            self._configuration = None
