"""core 包 - 向后兼容导出。"""

from .vrchat_errors import (
    VRChatClientError,
    VRChatTwoFactorRequiredError,
    VRChatAuthInvalidError,
    VRChatNetworkError,
    VRChatRateLimitedError,
)

__all__ = [
    "VRChatClientError",
    "VRChatTwoFactorRequiredError",
    "VRChatAuthInvalidError",
    "VRChatNetworkError",
    "VRChatRateLimitedError",
]
