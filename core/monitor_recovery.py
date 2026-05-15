"""监控服务自动恢复登录 Mixin。

本模块包含 MonitorService 的自动恢复登录逻辑，包括：
- 自动恢复登录主流程（restore_session → 账号密码重登）
- 指数退避策略管理
- 失败原因分类与记录
- 2FA 等待状态管理
- 恢复状态查询

由 MonitorService 通过多重继承使用，self 即为监控服务实例。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

from .vrchat_errors import (
    VRChatAuthInvalidError,
    VRChatClientError,
    VRChatNetworkError,
    VRChatTwoFactorRequiredError,
)

if TYPE_CHECKING:
    pass


class MonitorRecoveryMixin:
    """监控服务自动恢复登录 Mixin。"""

    def _prune_auto_recover_attempts(self, now_ts: float | None = None) -> None:
        now = now_ts or time.time()
        window_start = now - self._auto_recover_window_seconds
        self._auto_recover_attempt_timestamps = [ts for ts in self._auto_recover_attempt_timestamps if ts >= window_start]

    def _mark_auto_recover_result(self, result: str, reason: str = '') -> None:
        self._last_auto_recover_time = time.time()
        self._last_auto_recover_result = result
        self._last_auto_recover_reason = (reason or '').strip()

    def _reset_auto_recover_2fa_waiting(self) -> None:
        self._is_waiting_2fa_for_auto_recover = False
        self._auto_recover_pending_method = ''
        self._pending_logins.pop('__auto_recover__', None)

    def _classify_failure_reason(self, exc: Exception | None, fallback: str = '') -> tuple[str, str]:
        detail = str(exc or fallback or '').strip()
        text = detail.lower()
        if exc is not None:
            if isinstance(exc, VRChatAuthInvalidError) or self.client.is_auth_invalid_exception(exc):
                return 'auth invalid', detail
            if isinstance(exc, VRChatNetworkError):
                if 'timeout' in text or 'timed out' in text:
                    return 'timeout', detail
                return 'network', detail

        if '用户名或密码错误' in detail or 'invalid username' in text or 'invalid email' in text or 'invalid credentials' in text or 'bad credentials' in text:
            return 'invalid credentials', detail
        if 'timeout' in text or 'timed out' in text:
            return 'timeout', detail
        network_markers = ('connection', 'dns', 'ssl', 'network is unreachable', 'max retries exceeded')
        if any(marker in text for marker in network_markers):
            return 'network', detail
        if exc is not None and (isinstance(exc, VRChatAuthInvalidError) or self.client.is_auth_invalid_exception(exc)):
            return 'auth invalid', detail
        return 'unexpected', detail

    def _record_disconnect_reason(self, category: str, detail: str, source: str = '') -> None:
        self._last_disconnect_reason_category = (category or '').strip()
        self._last_disconnect_reason_detail = (detail or '').strip()
        self._last_disconnect_at = time.time()
        logger.warning('[vrc_friend_radar] 掉线原因记录 source=%s category=%s detail=%s', source or 'unknown', self._last_disconnect_reason_category or 'unknown', self._last_disconnect_reason_detail or 'n/a')

    async def _log_friends_api_readiness(self, stage: str) -> None:
        start = time.monotonic()
        try:
            await self.client.verify_session_ready(require_friends_api=True)
            elapsed = time.monotonic() - start
            logger.info('[vrc_friend_radar] 会话readiness观察 stage=%s ready=true elapsed=%.2fs', stage, elapsed)
        except Exception as exc:
            elapsed = time.monotonic() - start
            category, detail = self._classify_failure_reason(exc)
            logger.warning('[vrc_friend_radar] 会话readiness观察 stage=%s ready=false category=%s elapsed=%.2fs detail=%s (仅观察，不影响登录成功)', stage, category, elapsed, detail)

    def get_auto_recover_status(self) -> dict:
        self._prune_auto_recover_attempts()
        last_time = ''
        if self._last_auto_recover_time > 0:
            last_time = datetime.fromtimestamp(self._last_auto_recover_time).isoformat(timespec='seconds')
        next_allowed_text = ''
        if self._next_auto_recover_allowed_at > time.time():
            next_allowed_text = datetime.fromtimestamp(self._next_auto_recover_allowed_at).isoformat(timespec='seconds')
        return {
            'last_time': last_time,
            'last_result': self._last_auto_recover_result,
            'last_reason': self._last_auto_recover_reason,
            'waiting_2fa': self._is_waiting_2fa_for_auto_recover,
            'waiting_2fa_method': self._auto_recover_pending_method,
            'attempts_in_window': len(self._auto_recover_attempt_timestamps),
            'window_seconds': self._auto_recover_window_seconds,
            'max_attempts': self._auto_recover_max_attempts,
            # 新版指数退避状态
            'failure_count': self._auto_recover_failure_count,
            'backoff_seconds': list(self._auto_recover_backoff_seconds),
            'next_allowed_at': next_allowed_text,
            'exhausted': self._auto_recover_exhausted,
        }

    def _record_auto_recover_success(self) -> None:
        """自动恢复成功：清零失败计数和下一次允许时间。"""
        self._auto_recover_failure_count = 0
        self._next_auto_recover_allowed_at = 0.0
        self._auto_recover_exhausted = False

    def _record_auto_recover_failure(self, reason: str) -> None:
        """自动恢复失败：按退避表推进计数。

        失败次数 1 → 等 60s 才能再试
        失败次数 2 → 等 180s
        失败次数 3 → 等 300s
        失败次数 4+ → 永久停止，等管理员手动 /vrc登录
        """
        self._auto_recover_failure_count += 1
        now_ts = time.time()
        if self._auto_recover_failure_count > len(self._auto_recover_backoff_seconds):
            self._auto_recover_exhausted = True
            self._next_auto_recover_allowed_at = float('inf')
            logger.warning(
                '[vrc_friend_radar] 自动恢复已连续失败 %s 次（超过退避序列 %s），停止自动重登，等待管理员手动 /vrc登录。reason=%s',
                self._auto_recover_failure_count,
                self._auto_recover_backoff_seconds,
                reason,
            )
        else:
            wait = self._auto_recover_backoff_seconds[self._auto_recover_failure_count - 1]
            self._next_auto_recover_allowed_at = now_ts + wait
            logger.warning(
                '[vrc_friend_radar] 自动恢复失败第 %s 次，下一次允许时间推迟 %ss (=%s)。reason=%s',
                self._auto_recover_failure_count,
                wait,
                datetime.fromtimestamp(self._next_auto_recover_allowed_at).isoformat(timespec='seconds'),
                reason,
            )

    async def try_restore_session(self) -> bool:
        data = self.session_store.load()
        if not data:
            logger.info('[vrc_friend_radar] 启动恢复: 未找到 session.json，跳过 restore_session')
            self._mark_auto_recover_result('启动未恢复', '未找到 session.json')
            return False

        username = str(data.get('username', '') or '').strip()
        password = str(data.get('password', '') or '')
        cookie = str(data.get('cookie', '') or '').strip()
        if not username or not cookie:
            logger.warning('[vrc_friend_radar] 启动恢复: session.json 字段不完整(username/cookie)，跳过 restore_session')
            self._mark_auto_recover_result('启动未恢复', 'session.json 字段不完整(username/cookie)')
            return False

        if '=' not in cookie:
            logger.warning('[vrc_friend_radar] 启动恢复: session.json cookie 格式可疑（不包含 name=value）')

        try:
            logger.info('[vrc_friend_radar] 启动恢复: 检测到 session.json，开始执行 restore_session')
            result = await self.client.restore_session(username, password, cookie)
            self._last_login_result = result
            self.persist_session(force=True)
            self._reset_auto_recover_2fa_waiting()
            self._mark_auto_recover_result('启动恢复成功', '插件启动时restore_session成功')
            logger.info('[vrc_friend_radar] 启动恢复: restore_session 成功')
            return True
        except asyncio.CancelledError:
            self._last_persisted_cookie = ''
            self._mark_auto_recover_result('启动恢复跳过', 'restore_session 被取消或启动流程中断')
            logger.warning('[vrc_friend_radar] 启动恢复: restore_session 被取消/中断，本次自动恢复跳过，插件继续加载')
            return False
        except VRChatClientError as exc:
            self._recreate_client(preserve_credentials=False)
            self._last_persisted_cookie = ''
            self._mark_auto_recover_result('启动恢复失败', str(exc))
            logger.warning('[vrc_friend_radar] 启动恢复: restore_session 失败，已清理旧会话并准备账号密码重登。err=%s', exc)
            return False

    async def _try_periodic_health_check(self) -> None:
        if not self.client.is_logged_in():
            return
        now_ts = time.time()
        interval = getattr(self.cfg, 'low_frequency_health_check_seconds', 0) or self._health_check_interval_seconds
        interval = max(120, int(interval or self._health_check_interval_seconds))
        if (now_ts - self._last_health_check_at) < interval:
            return
        self._last_health_check_at = now_ts
        check_start = time.monotonic()
        logger.info('[vrc_friend_radar] 会话健康检查开始 interval=%ss', interval)
        try:
            try:
                ok = await self.client.probe_auth_token()
            except Exception:
                ok = False
            if not ok:
                ok = await self.client.probe_session_health()
            elapsed = time.monotonic() - check_start
            if ok:
                logger.info('[vrc_friend_radar] 会话健康检查通过 elapsed=%.2fs', elapsed)
                self._persist_session_if_cookie_changed()
        except Exception as exc:
            elapsed = time.monotonic() - check_start
            category, detail = self._classify_failure_reason(exc)
            logger.warning('[vrc_friend_radar] 会话健康检查失败 category=%s elapsed=%.2fs detail=%s', category, elapsed, detail)
            if category == 'auth invalid':
                self._record_disconnect_reason(category, detail, source='health_check')
                recovered = await self.auto_recover_login(f'低频健康检查发现认证失效: {detail}', trigger_exc=exc, source='health_check')
                if recovered:
                    logger.info('[vrc_friend_radar] 低频健康检查触发自动恢复成功')

    async def auto_recover_login(self, trigger_reason: str, trigger_exc: Exception | None = None, source: str = 'unknown') -> bool:
        async with self._auto_recover_lock:
            reason_text = (trigger_reason or '').strip() or '认证失效'
            category, detail = self._classify_failure_reason(trigger_exc, reason_text)
            logger.warning('[vrc_friend_radar] 自动恢复触发 source=%s category=%s reason=%s', source, category, detail)

            if self._is_waiting_2fa_for_auto_recover:
                pending_method = self._auto_recover_pending_method or 'unknown'
                self._mark_auto_recover_result('等待2FA', f'等待管理员提交验证码({pending_method})')
                logger.warning('[vrc_friend_radar] 自动恢复已暂停：当前等待2FA验证码，reason=%s', reason_text)
                return False

            now_ts = time.time()

            # ---- 指数退避：连续失败达上限后永久停止，直到管理员手动 /vrc登录 ----
            if self._auto_recover_exhausted:
                self._mark_auto_recover_result(
                    '已停止',
                    f'连续失败 {self._auto_recover_failure_count} 次（超过退避序列），仅管理员 /vrc登录 可以恢复',
                )
                logger.warning(
                    '[vrc_friend_radar] 自动恢复已停止：连续失败 %s 次。reason=%s',
                    self._auto_recover_failure_count,
                    reason_text,
                )
                return False

            # ---- 指数退避：还没到下一次允许尝试的时间点 ----
            if self._next_auto_recover_allowed_at > now_ts:
                remaining = int(self._next_auto_recover_allowed_at - now_ts)
                self._mark_auto_recover_result(
                    '退避中',
                    f'距离下一次尝试还有 {remaining}s（已失败 {self._auto_recover_failure_count} 次）',
                )
                logger.info(
                    '[vrc_friend_radar] 自动恢复退避中 remaining=%ss failure_count=%s reason=%s',
                    remaining,
                    self._auto_recover_failure_count,
                    reason_text,
                )
                return False

            # 兼容旧版滑动窗口统计（仅用于 /vrc状态 展示）
            self._prune_auto_recover_attempts(now_ts)
            self._auto_recover_attempt_timestamps.append(now_ts)

            self._mark_auto_recover_result('进行中', reason_text)
            logger.warning(
                '[vrc_friend_radar] 检测到认证失效，开始自动恢复登录。reason=%s, failure_count(before)=%s',
                reason_text,
                self._auto_recover_failure_count,
            )
            logger.info('[vrc_friend_radar] 自动恢复步骤 stage=discover_invalid_session source=%s category=%s', source, category)

            stored = self.session_store.load() or {}
            stored_username = str(stored.get('username', '') or '').strip()
            stored_password = str(stored.get('password', '') or '')
            stored_cookie = str(stored.get('cookie', '') or '').strip()

            if stored_username and stored_cookie:
                try:
                    logger.info('[vrc_friend_radar] 自动恢复步骤 stage=restore_session start user=%s', stored_username)
                    result = await self.client.restore_session(stored_username, stored_password, stored_cookie)
                    self._last_login_result = result
                    self.persist_session(force=True)
                    self._post_login_cooldown_until = time.time() + 60
                    self._last_health_check_at = time.time()
                    self._reset_auto_recover_2fa_waiting()
                    self._record_auto_recover_success()
                    self._mark_auto_recover_result('成功(restore)', '已通过 session_store 恢复')
                    logger.info('[vrc_friend_radar] 自动恢复步骤 stage=restore_session success')
                    await self._emit_notice('[VRC雷达] VRChat 登录状态已自动恢复（restore_session）。')
                    return True
                except VRChatClientError as exc:
                    logger.warning('[vrc_friend_radar] 自动恢复步骤 stage=restore_session failed detail=%s', exc)
                    logger.warning('[vrc_friend_radar] 自动恢复：restore_session失败，将执行清旧会话后重登。err=%s', exc)
                    self._recreate_client(preserve_credentials=False)
            else:
                logger.info('[vrc_friend_radar] 自动恢复：session_store信息不足，跳过restore_session')

            username, password = self.client.get_saved_credentials()
            username = (username or '').strip()
            password = (password or '')
            if not username or not password:
                username = stored_username

            # 关键增强：不要沿用可能污染的旧client/cookie，先清理会话再重建登录
            logger.info('[vrc_friend_radar] 自动恢复步骤 stage=recreate_client start')
            self._recreate_client(preserve_credentials=False)
            logger.info('[vrc_friend_radar] 自动恢复步骤 stage=recreate_client success')

            if not username or not password:
                reason = '无进程内账号密码，无法自动重登（安全策略不持久化本地密码）'
                self._record_auto_recover_failure(reason)
                self._mark_auto_recover_result('失败', reason)
                logger.error('[vrc_friend_radar] 自动恢复失败：%s', reason)
                await self._emit_notice(
                    '[VRC雷达] VRChat 登录状态恢复失败：当前无可用进程内账号密码（安全策略已禁用本地明文密码持久化），无法自动重登。\n'
                    '请管理员私聊执行 /vrc登录。'
                )
                return False

            try:
                logger.info('[vrc_friend_radar] 自动恢复步骤 stage=relogin start user=%s', username)
                result = await self.client.login(username=username, password=password)
                self._last_login_result = result
                self.persist_session(force=True)
                self._post_login_cooldown_until = time.time() + 60
                self._last_health_check_at = time.time()
                self._reset_auto_recover_2fa_waiting()
                self._record_auto_recover_success()
                self._mark_auto_recover_result('成功(relogin)', '已执行清旧会话后账号密码重登')
                logger.info('[vrc_friend_radar] 自动恢复步骤 stage=relogin success')
                await self._emit_notice('[VRC雷达] VRChat 登录状态已自动恢复（清旧会话后重登成功）。')
                return True
            except VRChatTwoFactorRequiredError as exc:
                # 2FA 属于"需要管理员介入"，不算一次失败，也不推进退避。
                self.create_pending_login(session_key='__auto_recover__', username=username, password=password, method=exc.method)
                self._is_waiting_2fa_for_auto_recover = True
                self._auto_recover_pending_method = exc.method
                self._mark_auto_recover_result('等待2FA', f'自动恢复重登需要2FA: {exc.method}')
                logger.warning('[vrc_friend_radar] 自动恢复步骤 stage=relogin wait_2fa method=%s', exc.method)
                await self._emit_notice(
                    f"[VRC雷达] 自动恢复登录需要二步验证（{exc.method}）。\n"
                    "请管理员私聊机器人发送：/vrc验证码 123456"
                )
                return False
            except VRChatClientError as exc:
                fail_category, fail_detail = self._classify_failure_reason(exc)
                self._record_disconnect_reason(fail_category, fail_detail, source='auto_recover_relogin_failed')
                self._record_auto_recover_failure(str(exc))
                self._mark_auto_recover_result('失败', str(exc))
                logger.error('[vrc_friend_radar] 自动恢复步骤 stage=relogin failed category=%s detail=%s', fail_category, fail_detail)

                # 给用户报告本次失败后会等多久再试
                if self._auto_recover_exhausted:
                    tail = '已连续失败多次，插件将停止自动重登，请管理员私聊执行 /vrc登录。'
                else:
                    wait = self._auto_recover_backoff_seconds[self._auto_recover_failure_count - 1]
                    tail = f'将在约 {wait // 60} 分钟后再尝试一次（指数退避）。'
                if isinstance(exc, VRChatNetworkError):
                    await self._emit_notice(f"[VRC雷达] 自动恢复登录失败：网络异常。\n详细：{exc}\n{tail}")
                else:
                    await self._emit_notice(f"[VRC雷达] 自动恢复登录失败：{exc}\n{tail}")
                return False
