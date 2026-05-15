"""VRChat 世界与实例 API Mixin。

本模块包含 VRChat API 客户端的世界信息查询、世界搜索、实例查询、
收藏世界列表、服务器状态等方法。
由 VRChatClient 通过多重继承使用，self 即为客户端实例。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .vrchat_errors import (
    VRChatClientError,
)

if TYPE_CHECKING:
    pass


class VRChatWorldMixin:
    """VRChat 世界与实例 API Mixin。"""

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
            world = api.get_world(world_id, _request_timeout=self._request_timeout_tuple())
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
            worlds = api.search_worlds(search=keyword, n=limit, offset=offset, _request_timeout=self._request_timeout_tuple())
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

    async def get_instance(self, world_id: str, instance_id: str) -> dict | None:
        return await asyncio.to_thread(self._get_instance_sync, world_id, instance_id)

    def _get_instance_sync(self, world_id: str, instance_id: str) -> dict | None:
        if not world_id or not instance_id or self._api_client is None:
            return None
        try:
            from vrchatapi.api import instances_api
        except ImportError:
            return None
        try:
            api = instances_api.InstancesApi(self._api_client)
            # 不同 SDK 版本参数顺序略不同，兼容处理
            get_instance = getattr(api, 'get_instance', None)
            if not callable(get_instance):
                return None
            try:
                inst = get_instance(world_id, instance_id, _request_timeout=self._request_timeout_tuple())
            except TypeError:
                inst = get_instance(world_id=world_id, instance_id=instance_id, _request_timeout=self._request_timeout_tuple())
            return {
                'world_id': world_id,
                'instance_id': instance_id,
                'n_users': int(getattr(inst, 'n_users', 0) or 0),
                'capacity': int(getattr(inst, 'capacity', 0) or 0),
                'recommended_capacity': int(getattr(inst, 'recommended_capacity', 0) or 0),
                'owner_id': self._to_text(getattr(inst, 'owner_id', '')),
                'region': self._to_text(getattr(inst, 'region', '')),
                'access_type': self._to_text(getattr(inst, 'type', '')),
                'full': bool(getattr(inst, 'full', False)),
                'closed_at': self._to_text(getattr(inst, 'closed_at', '')),
            }
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("获取实例信息失败", exc)
            return None

    async def list_favorite_worlds(self, n: int = 50) -> list[dict]:
        return await asyncio.to_thread(self._list_favorite_worlds_sync, n)

    def _list_favorite_worlds_sync(self, n: int) -> list[dict]:
        if self._api_client is None:
            return []
        try:
            from vrchatapi.api import worlds_api
        except ImportError:
            return []
        try:
            api = worlds_api.WorldsApi(self._api_client)
            method = getattr(api, 'get_favorited_worlds', None) or getattr(api, 'get_favorite_worlds', None)
            if not callable(method):
                return []
            worlds = method(n=max(1, min(int(n or 50), 100)), _request_timeout=self._request_timeout_tuple())
        except Exception as exc:
            if self._is_auth_invalid_exception(exc):
                self._raise_as_client_error("获取收藏世界失败", exc)
            return []
        result: list[dict] = []
        for item in worlds or []:
            result.append({
                'id': self._to_text(getattr(item, 'id', '')),
                'name': self._to_text(getattr(item, 'name', '')),
                'author_name': self._to_text(getattr(item, 'author_name', '')),
                'image_url': self._to_text(getattr(item, 'image_url', '')),
                'thumbnail_image_url': self._to_text(getattr(item, 'thumbnail_image_url', '')),
            })
        return result

    async def get_server_status(self) -> dict:
        """集合 SystemApi / MiscellaneousApi 的健康检查结果。"""
        return await asyncio.to_thread(self._get_server_status_sync)

    def _get_server_status_sync(self) -> dict:
        result: dict = {
            'ok': True,
            'server_time': '',
            'online_count': 0,
            'errors': [],
        }
        if self._api_client is None:
            result['ok'] = False
            result['errors'].append('尚未登录')
            return result
        try:
            from vrchatapi.api import system_api
        except ImportError:
            try:
                from vrchatapi.api import miscellaneous_api as system_api  # 老版本把这些接口叫 Miscellaneous
            except ImportError:
                result['ok'] = False
                result['errors'].append('SDK 未提供 system/miscellaneous api')
                return result

        try:
            api = system_api.SystemApi(self._api_client) if hasattr(system_api, 'SystemApi') else system_api.MiscellaneousApi(self._api_client)
        except Exception as exc:
            result['ok'] = False
            result['errors'].append(f'创建 system api 失败: {exc}')
            return result

        # server time
        try:
            server_time_method = getattr(api, 'get_system_time', None) or getattr(api, 'get_server_time', None)
            if callable(server_time_method):
                value = server_time_method(_request_timeout=self._request_timeout_tuple())
                if isinstance(value, str):
                    result['server_time'] = value
                else:
                    # 某些 SDK 返回 datetime，某些返回 {time: '...'} 这样的对象
                    result['server_time'] = self._to_text(getattr(value, 'time', '') or str(value))
        except Exception as exc:
            result['errors'].append(f'server_time: {exc}')

        # online count
        try:
            online_method = getattr(api, 'get_current_online_users', None)
            if callable(online_method):
                value = online_method(_request_timeout=self._request_timeout_tuple())
                # 可能直接是 int，也可能是对象
                if isinstance(value, (int, float)):
                    result['online_count'] = int(value)
                else:
                    count_attr = getattr(value, 'count', None) or getattr(value, 'online_users', None)
                    try:
                        result['online_count'] = int(count_attr) if count_attr is not None else int(value)
                    except Exception:
                        result['online_count'] = 0
        except Exception as exc:
            result['errors'].append(f'online_count: {exc}')

        if result['errors']:
            # 只要某个子项失败都不算 hard fail，保留 errors 方便调试
            pass
        return result
