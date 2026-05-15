"""VRChat 社交 API Mixin。

本模块包含 VRChat API 客户端的社交相关方法：好友请求、Boop、邀请、
用户搜索、用户详情、备注、用户组、黑名单等。
由 VRChatClient 通过多重继承使用，self 即为客户端实例。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

from .vrchat_errors import (
    VRChatClientError,
    VRChatRateLimitedError,
)

if TYPE_CHECKING:
    pass


class VRChatSocialMixin:
    """VRChat 社交 API Mixin。"""

    async def send_friend_request(self, user_id: str) -> dict:
        return await asyncio.to_thread(self._send_friend_request_sync, user_id)

    def _send_friend_request_sync(self, user_id: str) -> dict:
        target = str(user_id or '').strip()
        if not target:
            raise VRChatClientError("缺少目标用户ID")
        if self._api_client is None:
            raise VRChatClientError("尚未登录，无法发送好友请求")
        try:
            from vrchatapi.api import friends_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc
        try:
            api = friends_api.FriendsApi(self._api_client)
            resp = api.friend(target, _request_timeout=self._request_timeout_tuple())
            return {
                'id': str(getattr(resp, 'id', '') or ''),
                'type': str(getattr(resp, 'type', '') or ''),
                'sender_user_id': str(getattr(resp, 'sender_user_id', '') or ''),
                'receiver_user_id': str(getattr(resp, 'receiver_user_id', '') or ''),
            }
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("发送好友请求失败", exc)
            raise VRChatClientError(f"发送好友请求失败: {exc}") from exc

    async def respond_friend_request(self, notification_id: str, accept: bool) -> bool:
        """处理 friendRequest 通知：同意=/auth/user/notifications/:id/accept；拒绝=delete。"""
        return await asyncio.to_thread(self._respond_friend_request_sync, notification_id, accept)

    def _respond_friend_request_sync(self, notification_id: str, accept: bool) -> bool:
        target = str(notification_id or '').strip()
        if not target or self._api_client is None:
            raise VRChatClientError("尚未登录或缺少通知ID")
        try:
            from vrchatapi.api import notifications_api, friends_api
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc
        try:
            if accept:
                friends_client = friends_api.FriendsApi(self._api_client)
                method = getattr(friends_client, 'accept_friend_request', None) or getattr(friends_client, 'friend_request_accept', None)
                if callable(method):
                    method(target, _request_timeout=self._request_timeout_tuple())
                    return True
                # 最老版本 fallback：走 notifications.accept_friend_request
                notif_client = notifications_api.NotificationsApi(self._api_client)
                fallback = getattr(notif_client, 'accept_friend_request', None)
                if callable(fallback):
                    fallback(target, _request_timeout=self._request_timeout_tuple())
                    return True
                raise VRChatClientError("SDK 未提供 accept_friend_request 接口")

            notif_client = notifications_api.NotificationsApi(self._api_client)
            method = getattr(notif_client, 'delete_notification', None) or getattr(notif_client, 'clear_notification', None)
            if callable(method):
                method(target, _request_timeout=self._request_timeout_tuple())
                return True
            raise VRChatClientError("SDK 未提供 delete_notification 接口")
        except VRChatClientError:
            raise
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("处理好友请求通知失败", exc)
            raise VRChatClientError(f"处理好友请求通知失败: {exc}") from exc

    async def boop_user(self, user_id: str, emoji_id: str | None = None) -> dict:
        """调用 FriendsApi.boop 向好友发起一次 boop 互动。

        VRChat 官方 API 里 Boop 只承载 emoji/inventoryItem，**没有文字消息**。
        - `emoji_id` 传空：空 boop（对方只收到"被戳"通知，没有 emoji 粒子）
        - `emoji_id` 传字符串：可以是官方默认 emoji 的常量名（如 smile/skull/ghost 等），
          也可以是上传后的 FileID；由 VRChat 客户端具体识别。
        """
        return await asyncio.to_thread(self._boop_user_sync, user_id, emoji_id)

    def _boop_user_sync(self, user_id: str, emoji_id: str | None) -> dict:
        target = str(user_id or '').strip()
        if not target:
            raise VRChatClientError("缺少目标用户 ID")
        if self._api_client is None:
            raise VRChatClientError("尚未登录")

        # ---- 本地冷却：同目标短时间内第二次 boop 直接拒绝，不打 VRChat 服务 ----
        now_ts = time.time()
        next_allowed = self._boop_next_allowed_ts.get(target, 0.0)
        if next_allowed > now_ts:
            wait = int(next_allowed - now_ts) + 1
            raise VRChatRateLimitedError(
                f"刚刚才戳过对方，请等 {wait} 秒后再试（VRChat 服务端对同一目标的 Boop 有冷却）。",
                retry_after_seconds=wait,
            )

        try:
            from vrchatapi.api import friends_api
            from vrchatapi.models.boop_request import BoopRequest
        except ImportError as exc:
            raise VRChatClientError("当前 vrchatapi SDK 不支持 boop 接口，请升级依赖") from exc

        try:
            api = friends_api.FriendsApi(self._api_client)
            payload_kwargs: dict = {}
            emoji_value = str(emoji_id or '').strip()
            if emoji_value:
                payload_kwargs['emoji_id'] = emoji_value
            try:
                payload = BoopRequest(**payload_kwargs)
            except TypeError:
                payload = BoopRequest()
            resp = api.boop(target, payload, _request_timeout=self._request_timeout_tuple())
        except Exception as exc:
            status = self._extract_status_code(exc)
            if status == 429:
                # 尝试从 header 读 Retry-After
                retry_after = None
                try:
                    headers = getattr(exc, 'headers', None) or {}
                    header_val = headers.get('Retry-After') if hasattr(headers, 'get') else None
                    if header_val:
                        retry_after = int(str(header_val).strip())
                except Exception:
                    retry_after = None
                wait = retry_after if retry_after else int(self._boop_min_interval_seconds)
                # 把"下次允许时间"推到服务端建议值
                self._boop_next_allowed_ts[target] = time.time() + wait
                raise VRChatRateLimitedError(
                    f"VRChat 对这位好友的 Boop 正在冷却中（HTTP 429），请等约 {wait} 秒后再试。",
                    retry_after_seconds=wait,
                ) from exc
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("发送 boop 互动失败", exc)
            raise VRChatClientError(f"发送 boop 互动失败: {exc}") from exc

        # 成功才设置本地冷却窗口
        self._boop_next_allowed_ts[target] = time.time() + self._boop_min_interval_seconds
        return {
            'ok': True,
            'raw': getattr(resp, 'to_dict', lambda: {})() if resp is not None else {},
        }

    async def invite_user_to_instance(self, target_user_id: str, instance_id: str, message_slot: int | None = None) -> bool:
        return await asyncio.to_thread(self._invite_user_sync, target_user_id, instance_id, message_slot)

    def _invite_user_sync(self, target_user_id: str, instance_id: str, message_slot: int | None) -> bool:
        target = str(target_user_id or '').strip()
        location = str(instance_id or '').strip()
        if not target or not location:
            raise VRChatClientError("缺少目标用户或实例地址")
        if self._api_client is None:
            raise VRChatClientError("尚未登录")
        try:
            from vrchatapi.api import invite_api
            from vrchatapi.models.invite_request import InviteRequest
        except ImportError as exc:
            raise VRChatClientError("缺少 vrchatapi 依赖") from exc
        try:
            api = invite_api.InviteApi(self._api_client)
            payload = InviteRequest(instance_id=location)
            if message_slot is not None:
                setattr(payload, 'message_slot', int(message_slot))
            invite_method = getattr(api, 'invite_user', None) or getattr(api, 'invite_user_to_instance', None)
            if not callable(invite_method):
                raise VRChatClientError("SDK 未提供 invite_user 接口")
            invite_method(target, payload, _request_timeout=self._request_timeout_tuple())
            return True
        except VRChatClientError:
            raise
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("发送实例邀请失败", exc)
            raise VRChatClientError(f"发送实例邀请失败: {exc}") from exc

    async def search_users(self, keyword: str, limit: int = 10, offset: int = 0) -> list[dict]:
        return await asyncio.to_thread(self._search_users_sync, keyword, limit, offset)

    def _search_users_sync(self, keyword: str, limit: int, offset: int) -> list[dict]:
        keyword = str(keyword or '').strip()
        if not keyword or self._api_client is None:
            return []
        try:
            from vrchatapi.api import users_api
        except ImportError:
            return []
        try:
            api = users_api.UsersApi(self._api_client)
            users = api.search_users(
                search=keyword,
                n=max(1, min(int(limit or 10), 60)),
                offset=max(0, int(offset or 0)),
                _request_timeout=self._request_timeout_tuple(),
            )
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("搜索用户失败", exc)
            return []
        result: list[dict] = []
        for item in users or []:
            result.append({
                'id': self._to_text(getattr(item, 'id', '')),
                'display_name': self._to_text(getattr(item, 'display_name', '')),
                'username': self._to_text(getattr(item, 'username', '')),
                'status': self._to_text(getattr(item, 'status', '')),
                'bio': self._to_text(getattr(item, 'bio', '')),
                'profile_pic_override': self._to_text(getattr(item, 'profile_pic_override', '')),
            })
        return result

    async def get_user_detail(self, user_id: str) -> dict | None:
        """调用 UsersApi.get_user 获取目标用户公开资料。"""
        return await asyncio.to_thread(self._get_user_detail_sync, user_id)

    def _get_user_detail_sync(self, user_id: str) -> dict | None:
        target = str(user_id or '').strip()
        if not target or self._api_client is None:
            return None
        try:
            from vrchatapi.api import users_api
        except ImportError:
            return None
        try:
            api = users_api.UsersApi(self._api_client)
            user = api.get_user(target, _request_timeout=self._request_timeout_tuple())
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("获取用户资料失败", exc)
            return None
        if user is None:
            return None
        # 尽量多拿字段，方便插件侧做画像展示
        tags_raw = getattr(user, 'tags', []) or []
        tags = [self._to_text(t) for t in tags_raw if self._to_text(t)]
        return {
            'id': self._to_text(getattr(user, 'id', '')),
            'display_name': self._to_text(getattr(user, 'display_name', '')),
            'username': self._to_text(getattr(user, 'username', '')),
            'status': self._to_text(getattr(user, 'status', '')),
            'status_description': self._to_text(getattr(user, 'status_description', '')),
            'bio': self._to_text(getattr(user, 'bio', '')),
            'bio_links': [self._to_text(link) for link in (getattr(user, 'bio_links', []) or [])],
            'location': self._to_text(getattr(user, 'location', '')),
            'world_id': self._to_text(getattr(user, 'world_id', '')),
            'instance_id': self._to_text(getattr(user, 'instance_id', '')),
            'date_joined': self._to_text(getattr(user, 'date_joined', '')),
            'last_login': self._to_text(getattr(user, 'last_login', '')),
            'last_activity': self._to_text(getattr(user, 'last_activity', '')),
            'last_platform': self._to_text(getattr(user, 'last_platform', '')),
            'platform': self._to_text(getattr(user, 'platform', '')),
            'profile_pic_override': self._to_text(getattr(user, 'profile_pic_override', '')),
            'current_avatar_image_url': self._to_text(getattr(user, 'current_avatar_image_url', '')),
            'current_avatar_thumbnail_image_url': self._to_text(getattr(user, 'current_avatar_thumbnail_image_url', '')),
            'user_icon': self._to_text(getattr(user, 'user_icon', '')),
            'age_verification_status': self._to_text(getattr(user, 'age_verification_status', '')),
            'is_friend': bool(getattr(user, 'is_friend', False)),
            'tags': tags,
        }

    async def update_user_note(self, target_user_id: str, note_text: str) -> dict | None:
        """把备注写到 VRChat 账号（UsersApi.update_user_note），让备注在官网也可见。"""
        return await asyncio.to_thread(self._update_user_note_sync, target_user_id, note_text)

    def _update_user_note_sync(self, target_user_id: str, note_text: str) -> dict | None:
        target = str(target_user_id or '').strip()
        if not target or self._api_client is None:
            return None
        try:
            from vrchatapi.api import users_api
            from vrchatapi.models.update_user_note_request import UpdateUserNoteRequest
        except ImportError:
            return None
        try:
            api = users_api.UsersApi(self._api_client)
            payload = UpdateUserNoteRequest(target_user_id=target, note=str(note_text or ''))
            resp = api.update_user_note(payload, _request_timeout=self._request_timeout_tuple())
            to_dict = getattr(resp, 'to_dict', None)
            if callable(to_dict):
                return to_dict()
            return {'note': self._to_text(getattr(resp, 'note', ''))}
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("更新用户备注失败", exc)
            logger.warning(f"[vrc_friend_radar] update_user_note 失败: {exc}")
            return None

    async def list_user_groups(self, user_id: str | None = None) -> list[dict]:
        return await asyncio.to_thread(self._list_user_groups_sync, user_id)

    def _list_user_groups_sync(self, user_id: str | None) -> list[dict]:
        if self._api_client is None:
            return []
        try:
            from vrchatapi.api import users_api
        except ImportError:
            return []
        target = str(user_id or self._current_user_id or '').strip()
        if not target:
            return []
        try:
            api = users_api.UsersApi(self._api_client)
            fetch = getattr(api, 'get_user_groups', None)
            if not callable(fetch):
                return []
            groups = fetch(target, _request_timeout=self._request_timeout_tuple())
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("获取用户组列表失败", exc)
            return []
        result: list[dict] = []
        for item in groups or []:
            result.append({
                'id': self._to_text(getattr(item, 'id', '') or getattr(item, 'group_id', '')),
                'name': self._to_text(getattr(item, 'name', '')),
                'short_code': self._to_text(getattr(item, 'short_code', '')),
                'member_count': int(getattr(item, 'member_count', 0) or 0),
            })
        return result

    async def list_blocked_user_ids(self) -> list[str]:
        return await asyncio.to_thread(self._list_blocked_user_ids_sync)

    def _list_blocked_user_ids_sync(self) -> list[str]:
        if self._api_client is None:
            return []
        try:
            from vrchatapi.api import playermoderation_api
        except ImportError:
            return []
        try:
            api = playermoderation_api.PlayermoderationApi(self._api_client)
            moderations = api.get_player_moderations(_request_timeout=self._request_timeout_tuple())
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("获取玩家黑名单失败", exc)
            return []
        blocked: list[str] = []
        for item in moderations or []:
            mod_type = self._to_text(getattr(item, 'type', '')).lower()
            target_id = self._to_text(getattr(item, 'target_user_id', ''))
            if not target_id:
                continue
            if mod_type in {'block', 'mute', 'hidemute', 'hideavatar'}:
                blocked.append(target_id)
        return sorted(set(blocked))
