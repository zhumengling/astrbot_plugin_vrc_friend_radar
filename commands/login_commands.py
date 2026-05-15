"""登录相关命令 Mixin。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.vrchat_errors import VRChatClientError, VRChatNetworkError, VRChatTwoFactorRequiredError

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class LoginCommandsMixin:
    """登录相关命令 Mixin。

    由 VRCFriendRadarPlugin 继承使用，self 即为插件实例。
    """

    def _parse_login_credentials(self: 'VRCFriendRadarPlugin', message_text: str) -> tuple[str, str]:
        # 兼容旧格式：/vrc登录 用户名 密码
        # 新逻辑：第一个参数作为用户名，其后全文原样作为密码（尽量保留空白与特殊符号）
        raw = str(message_text or '').replace("vrc登录", "", 1)
        payload = raw.lstrip()
        if not payload:
            return '', ''

        # 仅按"第一个空白字符"切分一次：
        # - 用户名：首段非空白
        # - 密码：其后全文原样（可包含空格、#、@、:、CQ转义后的字符等）
        split_idx = -1
        for idx, ch in enumerate(payload):
            if ch.isspace():
                split_idx = idx
                break
        if split_idx <= 0:
            return '', ''

        username = payload[:split_idx]
        password = payload[split_idx + 1:]
        return username, password

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc登录")
    async def interactive_login(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        if self._get_group_id(event):
            yield event.plain_result("为了账号安全，请私聊 Bot 发送登录账号和密码，不要在群里发送。")
            return
        self._remember_private_admin_sender(event)
        username, password = self._parse_login_credentials(event.message_str)
        if not username or password == '':
            yield event.plain_result("用法：/vrc登录 用户名 密码")
            return
        logger.info(
            "[vrc_friend_radar] 登录命令解析: username=%s(len=%s, email_like=%s), password_len=%s, contains_ws=%s, leading_ws=%s, trailing_ws=%s",
            username,
            len(username),
            ('@' in username),
            len(password),
            any(ch.isspace() for ch in password),
            bool(password and password[0].isspace()),
            bool(password and password[-1].isspace()),
        )
        session_key = self._build_session_key(event)
        timeout_seconds = self.cfg.login_session_timeout_seconds
        attempt_id = self.monitor.create_manual_login_attempt()
        yield event.plain_result("已收到登录请求，正在连接 VRChat，请稍候…")
        login_task = asyncio.create_task(
            self.monitor.test_login(
                username=username,
                password=password,
                attempt_id=attempt_id,
            )
        )
        self._track_background_task(login_task, f"manual_login:{session_key}")
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(login_task), timeout=10)
            except asyncio.TimeoutError:
                yield event.plain_result("登录请求处理中，VRChat 可能响应较慢，请继续稍候…")
                try:
                    result = await asyncio.wait_for(asyncio.shield(login_task), timeout=max(5, timeout_seconds))
                except asyncio.TimeoutError:
                    self.monitor.abandon_manual_login_attempt(attempt_id)
                    logger.error(f"[vrc_friend_radar] 登录任务超时(>10s + {max(5, timeout_seconds)}s)，后台线程可能阻塞")
                    yield event.plain_result("登录长时间未完成，已放弃本次等待；即使后台任务稍后结束，也不会覆盖当前会话。请稍后重试；若持续复现，请查看日志中的登录阶段(stage)定位卡点。")
                    return
            yield event.plain_result(f"VRChat 登录成功\n用户ID: {result.user_id}\n显示名: {result.display_name}")
            for message in await self._post_login_auto_sync_and_reply(event):
                yield event.plain_result(message)
        except asyncio.TimeoutError:
            self.monitor.abandon_manual_login_attempt(attempt_id)
            logger.error("[vrc_friend_radar] 登录流程超时（等待初始10秒提示阶段）")
            yield event.plain_result("登录请求超时，已放弃本次等待且不会污染当前会话。若持续超时，请检查网络或 VRChat 服务状态。")
        except VRChatTwoFactorRequiredError as exc:
            self.monitor.create_pending_login(session_key=session_key, username=username, password=password, method=exc.method)
            if exc.method == "totp_or_recovery":
                yield event.plain_result(f"检测到二步验证，请在{timeout_seconds}秒内发送动态验证码或恢复码：/vrc验证码 123456")
                return
            if exc.method == "email":
                yield event.plain_result(f"检测到邮箱验证码验证，请在{timeout_seconds}秒内发送：/vrc验证码 123456")
                return
            yield event.plain_result(f"检测到额外验证方式 {exc.method}，请在{timeout_seconds}秒内发送：/vrc验证码 123456")
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 登录失败: {exc}")
            if isinstance(exc, VRChatNetworkError):
                yield event.plain_result(f"VRChat 登录失败：网络异常或超时。\n详情：{exc}\n请检查网络或稍后重试。")
            else:
                yield event.plain_result(f"VRChat 登录失败：{exc}")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 登录流程发生未预期异常: {exc}")
            yield event.plain_result("登录流程异常，请稍后重试或查看日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc验证码")
    async def submit_code(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        if self._get_group_id(event):
            yield event.plain_result("为了账号安全，请私聊 Bot 发送验证码，不要在群里发送。")
            return
        self._remember_private_admin_sender(event)
        code = event.message_str.replace("vrc验证码", "", 1).strip()
        if not code:
            yield event.plain_result("用法：/vrc验证码 123456")
            return
        session_key = self._build_session_key(event)
        pending_key = session_key
        pending = self.monitor.get_pending_login(pending_key)
        if not pending:
            # 兜底：运行中自动恢复触发2FA时，允许管理员在任意私聊上下文提交验证码
            pending_key = '__auto_recover__'
            pending = self.monitor.get_pending_login(pending_key)
        if not pending:
            yield event.plain_result("当前没有等待验证的登录会话，请先发送：/vrc登录 用户名 密码")
            return
        attempt_id = self.monitor.create_manual_login_attempt()
        yield event.plain_result("已收到验证码，正在提交验证，请稍候…")
        login_task = asyncio.create_task(
            self.monitor.test_login(
                username=pending.username,
                password=pending.password,
                two_factor_code=code,
                attempt_id=attempt_id,
            )
        )
        self._track_background_task(login_task, f"manual_login_2fa:{pending_key}")
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(login_task), timeout=10)
            except asyncio.TimeoutError:
                yield event.plain_result("验证码验证处理中，VRChat 可能响应较慢，请继续稍候…")
                try:
                    result = await asyncio.wait_for(asyncio.shield(login_task), timeout=max(5, self.cfg.login_session_timeout_seconds))
                except asyncio.TimeoutError:
                    self.monitor.abandon_manual_login_attempt(attempt_id)
                    logger.error(f"[vrc_friend_radar] 验证码提交任务超时(>10s + {max(5, self.cfg.login_session_timeout_seconds)}s)，后台线程可能阻塞")
                    yield event.plain_result("验证码提交后长时间未完成，已放弃本次等待；即使后台任务稍后结束，也不会覆盖当前会话。请重试 /vrc验证码 123456，必要时重新 /vrc登录。")
                    return
            self.monitor.pop_pending_login(pending_key)
            yield event.plain_result(f"VRChat 登录成功\n用户ID: {result.user_id}\n显示名: {result.display_name}")
            for message in await self._post_login_auto_sync_and_reply(event):
                yield event.plain_result(message)
        except asyncio.TimeoutError:
            self.monitor.abandon_manual_login_attempt(attempt_id)
            logger.error("[vrc_friend_radar] 验证码流程超时（等待初始10秒提示阶段）")
            yield event.plain_result("验证码验证超时，已放弃本次等待且不会污染当前会话。若仍失败，可重新执行 /vrc登录。")
        except VRChatClientError as exc:
            logger.error(f"[vrc_friend_radar] 验证码登录失败: {exc}")
            if isinstance(exc, VRChatNetworkError):
                yield event.plain_result(f"验证码登录失败：网络异常或超时。\n详情：{exc}\n可直接重试 /vrc验证码 123456。")
            else:
                yield event.plain_result(f"验证码登录失败：{exc}，你可以直接重新发送 /vrc验证码 123456 重试。")
        except Exception as exc:
            logger.exception(f"[vrc_friend_radar] 验证码流程发生未预期异常: {exc}")
            yield event.plain_result("验证码处理异常，请稍后重试或重新执行 /vrc登录。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc解绑登录")
    async def clear_login(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        self.monitor.clear_persisted_session()
        yield event.plain_result("已清除持久化登录态。")


