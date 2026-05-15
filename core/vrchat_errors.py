"""VRChat API 错误类定义。

本模块包含 VRChat API 客户端使用的所有自定义异常类，
供 vrchat_client.py 及其他模块引用，避免循环导入。
"""

from __future__ import annotations


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


class VRChatRateLimitedError(VRChatClientError):
    """HTTP 429 / 服务端冷却窗口内。携带建议的等待秒数。"""
    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = int(retry_after_seconds) if retry_after_seconds else None
