import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from .config import PluginConfig
from .db import RadarDB
from .diff import diff_snapshot
from .models import FriendSnapshot, RadarEvent
from .notifier import Notifier
from .repository import SettingsRepository
from .session_store import SessionStore
from .utils import get_location_group_key, infer_joinability
from astrbot.api import logger
from .vrchat_client import (
    LoginResult,
    VRChatAuthInvalidError,
    VRChatClient,
    VRChatClientError,
    VRChatNetworkError,
    VRChatTwoFactorRequiredError,
)


@dataclass(slots=True)
class PendingLoginSession:
    session_key: str
    username: str
    password: str
    created_at: float
    method: str = "unknown"

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.created_at) > ttl_seconds


class MonitorService:
    def __init__(self, cfg: PluginConfig, db: RadarDB, settings_repo: SettingsRepository):
        self.cfg = cfg
        self.db = db
        self.settings_repo = settings_repo
        self.notifier = Notifier()
        self.client = VRChatClient(cfg.vrchat_user_agent)
        self.session_store = SessionStore(cfg.data_dir)
        self._task: asyncio.Task | None = None
        self._running = False
        self._tick_count = 0
        self._last_login_result: LoginResult | None = None
        self._pending_logins: dict[str, PendingLoginSession] = {}
        self._last_sync_count = 0
        self._last_detected_events: list[RadarEvent] = []
        self._event_callback: Callable[[list[RadarEvent]], Awaitable[None]] | None = None
        self._loop_tick_callback: Callable[[datetime], Awaitable[None]] | None = None
        self._notice_callback: Callable[[str], Awaitable[None]] | None = None
        self._last_coroom_notify_at: dict[str, float] = {}
        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(self.cfg.read_notify_group_ids_from_raw())
        self._last_seen_raw_watch_friends = self._dedupe_clean_ids(self.cfg.read_watch_friend_ids_from_raw())
        self._stop_event = asyncio.Event()
        self._auto_recover_window_seconds = 3600
        self._auto_recover_max_attempts = 3
        self._auto_recover_attempt_timestamps: list[float] = []
        self._last_auto_recover_time: float = 0.0
        self._last_auto_recover_result: str = '未触发'
        self._last_auto_recover_reason: str = ''
        self._is_waiting_2fa_for_auto_recover: bool = False
        self._auto_recover_pending_method: str = ''
        self._session_persist_interval_seconds = 300
        self._health_check_interval_seconds = 1800
        self._last_session_persist_at: float = 0.0
        self._last_health_check_at: float = 0.0
        self._last_persisted_cookie: str = ''
        self._auto_recover_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._last_self_presence_status: str = ''
        self._last_self_presence_location: str = ''
        self._last_self_presence_updated_at: str = ''
        self._last_disconnect_reason_category: str = ''
        self._last_disconnect_reason_detail: str = ''
        self._last_disconnect_at: float = 0.0
        self._last_self_snapshot_failure_reason: str = ''
        self._last_self_snapshot_failure_stage: str = ''
        self._last_self_snapshot_failure_at: float = 0.0
        self._last_self_snapshot_success_at: float = 0.0
        self._self_snapshot_failure_streak: int = 0
        self._post_login_cooldown_until: float = 0.0
        self._manual_login_attempts: set[str] = set()

    def _recreate_client(self, preserve_credentials: bool = True) -> None:
        username = password = ""
        if preserve_credentials:
            username, password = self.client.get_saved_credentials()
        self.client.close()
        self.client = VRChatClient(self.cfg.vrchat_user_agent)
        if preserve_credentials and username and password:
            self.client._username = username
            self.client._password = password

    def create_manual_login_attempt(self) -> str:
        attempt_id = f"manual-login-{time.time_ns()}"
        self._manual_login_attempts.add(attempt_id)
        return attempt_id

    def abandon_manual_login_attempt(self, attempt_id: str | None) -> None:
        target = str(attempt_id or '').strip()
        if target:
            self._manual_login_attempts.discard(target)

    def _adopt_logged_in_client(self, logged_in_client: VRChatClient) -> None:
        previous_client = self.client
        self.client = logged_in_client
        previous_client.close()

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
            # 启动阶段 restore_session 若被取消（插件卸载/热重载/初始化中断），不应打崩插件加载。
            # 此处将取消视为“本次自动恢复跳过”，后续仍允许自动重登或手动登录。
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
            # restore 失败时优先清理旧会话状态，避免后续沿用脏 client/cookie。
            self._recreate_client(preserve_credentials=False)
            self._last_persisted_cookie = ''
            self._mark_auto_recover_result('启动恢复失败', str(exc))
            logger.warning('[vrc_friend_radar] 启动恢复: restore_session 失败，已清理旧会话并准备账号密码重登。err=%s', exc)
            return False

    def persist_session(self, force: bool = False) -> bool:
        data = self.client.export_session()
        if not data:
            logger.warning('[vrc_friend_radar] persist_session skipped: export_session returned empty (not logged in or cookie unavailable)')
            return False
        cookie = str(data.get('cookie', '') or '')
        if not cookie:
            logger.warning('[vrc_friend_radar] persist_session skipped: exported session has empty cookie')
            return False
        if not force and cookie == self._last_persisted_cookie:
            return False
        self.session_store.save(data)
        self._last_persisted_cookie = cookie
        self._last_session_persist_at = time.time()
        logger.info('[vrc_friend_radar] persist_session ok: session.json updated')
        return True

    def _persist_session_if_cookie_changed(self) -> bool:
        return self.persist_session(force=False)

    async def _try_periodic_session_persist(self) -> None:
        if not self.client.is_logged_in():
            return
        now_ts = time.time()
        if (now_ts - self._last_session_persist_at) < self._session_persist_interval_seconds:
            return
        changed = self.persist_session(force=False)
        if not changed:
            # 即使cookie未变化，也更新检查时间，避免无意义高频导出
            self._last_session_persist_at = now_ts

    async def _try_periodic_health_check(self) -> None:
        if not self.client.is_logged_in():
            return
        now_ts = time.time()
        if (now_ts - self._last_health_check_at) < self._health_check_interval_seconds:
            return
        self._last_health_check_at = now_ts
        check_start = time.monotonic()
        logger.info('[vrc_friend_radar] 会话健康检查开始 interval=%ss', self._health_check_interval_seconds)
        try:
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

    def clear_persisted_session(self) -> None:
        self.session_store.clear()
        self._last_persisted_cookie = ''
        self._last_session_persist_at = 0.0

    def set_event_callback(self, callback: Callable[[list[RadarEvent]], Awaitable[None]]) -> None:
        self._event_callback = callback

    def set_loop_tick_callback(self, callback: Callable[[datetime], Awaitable[None]]) -> None:
        self._loop_tick_callback = callback

    def set_notice_callback(self, callback: Callable[[str], Awaitable[None]] | None) -> None:
        self._notice_callback = callback

    async def _emit_notice(self, message: str) -> None:
        text = str(message or '').strip()
        if not text:
            return
        if not self._notice_callback:
            logger.warning('[vrc_friend_radar] 状态通知(未配置通知回调): %s', text)
            return
        try:
            await self._notice_callback(text)
        except Exception as exc:
            logger.error('[vrc_friend_radar] 状态通知发送失败: %s', exc, exc_info=True)

    def get_effective_notify_groups(self) -> list[str]:
        raw_groups = self._dedupe_clean_ids(self.cfg.read_notify_group_ids_from_raw())
        runtime_groups = self._dedupe_clean_ids(self.cfg.notify_group_ids)
        repo_groups = self._dedupe_clean_ids(self.settings_repo.get_notify_groups())
        # 外部配置发生变更（WebUI/配置热更新）：以当前配置视图为准，覆盖到持久化
        if raw_groups != self._last_seen_raw_notify_groups:
            self.settings_repo.set_notify_groups(raw_groups)
            self.cfg.sync_runtime_lists(notify_group_ids=raw_groups, write_back_raw=True)
            result = raw_groups
        else:
            # 命令/数据库改动：以持久化为准同步到运行时配置
            if repo_groups != runtime_groups:
                self.cfg.sync_runtime_lists(notify_group_ids=repo_groups, write_back_raw=True)
                result = repo_groups
            else:
                result = runtime_groups

        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(self.cfg.read_notify_group_ids_from_raw())
        return result

    def get_effective_watch_friends(self) -> list[str]:
        raw_friend_ids = self._dedupe_clean_ids(self.cfg.read_watch_friend_ids_from_raw())
        runtime_friend_ids = self._dedupe_clean_ids(self.cfg.watch_friend_ids)
        repo_friend_ids = self._dedupe_clean_ids(self.settings_repo.get_watch_friends())
        # 外部配置发生变更（WebUI/配置热更新）：以当前配置视图为准，覆盖到持久化
        if raw_friend_ids != self._last_seen_raw_watch_friends:
            self.settings_repo.set_watch_friends(raw_friend_ids)
            self.cfg.sync_runtime_lists(watch_friend_ids=raw_friend_ids, write_back_raw=True)
            result = raw_friend_ids
        else:
            # 命令/数据库改动：以持久化为准同步到运行时配置
            if repo_friend_ids != runtime_friend_ids:
                self.cfg.sync_runtime_lists(watch_friend_ids=repo_friend_ids, write_back_raw=True)
                result = repo_friend_ids
            else:
                result = runtime_friend_ids

        self._last_seen_raw_watch_friends = self._dedupe_clean_ids(self.cfg.read_watch_friend_ids_from_raw())

        # 监控列表对外展示时，watch_self=true 需体现到 effective list（不写回配置/数据库）
        if self.cfg.watch_self:
            self_id = (self.client.get_current_user_id() or '').strip()
            if self_id:
                result = self._dedupe_clean_ids([*result, self_id])
        return result

    def get_monitor_watch_friend_ids(self) -> list[str]:
        """监控语义统一入口：监控名单 + 可选本人。"""
        # get_effective_watch_friends 已处理 watch_self 的可见性，这里仅做一次去重。
        return self._dedupe_clean_ids(self.get_effective_watch_friends())

    @staticmethod
    def _should_track_self_location_change(old_location: str | None, new_location: str | None) -> bool:
        # 本人监控时，location 不可见/未知/私密等场景噪声较高，仅跟踪可识别世界实例之间的切换
        return bool(get_location_group_key(old_location)) and bool(get_location_group_key(new_location))

    @staticmethod
    def _dedupe_clean_ids(friend_ids: list[str] | None) -> list[str]:
        items: list[str] = []
        seen: set[str] = set()
        for friend_id in friend_ids or []:
            value = str(friend_id or '').strip()
            if not value or value in seen:
                continue
            seen.add(value)
            items.append(value)
        return items

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
        return {
            'last_time': last_time,
            'last_result': self._last_auto_recover_result,
            'last_reason': self._last_auto_recover_reason,
            'waiting_2fa': self._is_waiting_2fa_for_auto_recover,
            'waiting_2fa_method': self._auto_recover_pending_method,
            'attempts_in_window': len(self._auto_recover_attempt_timestamps),
            'window_seconds': self._auto_recover_window_seconds,
            'max_attempts': self._auto_recover_max_attempts,
        }

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
            self._prune_auto_recover_attempts(now_ts)
            if len(self._auto_recover_attempt_timestamps) >= self._auto_recover_max_attempts:
                self._mark_auto_recover_result('节流跳过', f'{self._auto_recover_window_seconds // 60}分钟内自动恢复次数已达上限({self._auto_recover_max_attempts})')
                logger.warning('[vrc_friend_radar] 自动恢复触发节流，已跳过本轮。reason=%s, attempts=%s/%s', reason_text, len(self._auto_recover_attempt_timestamps), self._auto_recover_max_attempts)
                await self._emit_notice(
                    f"[VRC雷达] 登录状态恢复触发节流：{self._auto_recover_window_seconds // 60} 分钟内已尝试 {self._auto_recover_max_attempts} 次。\n"
                    f"触发原因：{reason_text}\n"
                    "请稍后重试，或管理员手动执行 /vrc登录。"
                )
                return False

            self._auto_recover_attempt_timestamps.append(now_ts)
            self._mark_auto_recover_result('进行中', reason_text)
            logger.warning('[vrc_friend_radar] 检测到认证失效，开始自动恢复登录。reason=%s, attempt=%s/%s', reason_text, len(self._auto_recover_attempt_timestamps), self._auto_recover_max_attempts)
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
                    self._mark_auto_recover_result('成功(restore_session)', '已通过持久化session恢复')
                    logger.info('[vrc_friend_radar] 自动恢复步骤 stage=restore_session success')
                    logger.info('[vrc_friend_radar] 自动恢复成功：restore_session')
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
                # 安全修复后不再从 session.json 回读明文密码，仅允许使用当前进程内已保存凭据

            # 关键增强：不要沿用可能污染的旧client/cookie，先清理会话再重建登录
            logger.info('[vrc_friend_radar] 自动恢复步骤 stage=recreate_client start')
            self._recreate_client(preserve_credentials=False)
            logger.info('[vrc_friend_radar] 自动恢复步骤 stage=recreate_client success')

            if not username or not password:
                self._mark_auto_recover_result('失败', '无进程内账号密码，无法自动重登（安全策略不持久化本地密码）')
                logger.error('[vrc_friend_radar] 自动恢复失败：无进程内账号密码（session.json 不再持久化密码）')
                await self._emit_notice('[VRC雷达] VRChat 登录状态恢复失败：当前无可用进程内账号密码（安全策略已禁用本地明文密码持久化），无法自动重登。\n请管理员私聊执行 /vrc登录。')
                return False

            try:
                logger.info('[vrc_friend_radar] 自动恢复步骤 stage=relogin start user=%s', username)
                result = await self.client.login(username=username, password=password)
                self._last_login_result = result
                self.persist_session(force=True)
                self._post_login_cooldown_until = time.time() + 60
                self._last_health_check_at = time.time()
                self._reset_auto_recover_2fa_waiting()
                self._mark_auto_recover_result('成功(relogin)', '已执行清旧会话后账号密码重登')
                logger.info('[vrc_friend_radar] 自动恢复步骤 stage=relogin success')
                logger.info('[vrc_friend_radar] 自动恢复成功：清旧会话后账号密码重登成功')
                await self._emit_notice('[VRC雷达] VRChat 登录状态已自动恢复（清旧会话后重登成功）。')
                return True
            except VRChatTwoFactorRequiredError as exc:
                self.create_pending_login(session_key='__auto_recover__', username=username, password=password, method=exc.method)
                self._is_waiting_2fa_for_auto_recover = True
                self._auto_recover_pending_method = exc.method
                self._mark_auto_recover_result('等待2FA', f'自动恢复重登需要2FA: {exc.method}')
                logger.warning('[vrc_friend_radar] 自动恢复步骤 stage=relogin wait_2fa method=%s', exc.method)
                logger.warning('[vrc_friend_radar] 自动恢复重登需要2FA，已进入待验证码状态。method=%s', exc.method)
                await self._emit_notice(
                    f"[VRC雷达] 自动恢复登录需要二步验证（{exc.method}）。\n"
                    "请管理员私聊机器人发送：/vrc验证码 123456"
                )
                return False
            except VRChatClientError as exc:
                self._mark_auto_recover_result('失败', str(exc))
                fail_category, fail_detail = self._classify_failure_reason(exc)
                self._record_disconnect_reason(fail_category, fail_detail, source='auto_recover_relogin_failed')
                logger.error('[vrc_friend_radar] 自动恢复步骤 stage=relogin failed category=%s detail=%s', fail_category, fail_detail)
                logger.error('[vrc_friend_radar] 自动恢复重登失败: %s', exc)
                if isinstance(exc, VRChatNetworkError):
                    await self._emit_notice(f"[VRC雷达] 自动恢复登录失败：网络异常。\n详细：{exc}\n请稍后重试或检查网络连通性。")
                else:
                    await self._emit_notice(f"[VRC雷达] 自动恢复登录失败：{exc}\n请管理员私聊执行 /vrc登录。")
                return False




    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()

        restored = False
        restore_timeout_seconds = 45
        try:
            restored = await asyncio.wait_for(self.try_restore_session(), timeout=restore_timeout_seconds)
        except asyncio.TimeoutError:
            self._mark_auto_recover_result('启动恢复超时', f'restore_session 超过 {restore_timeout_seconds}s，已跳过')
            logger.warning('[vrc_friend_radar] 启动恢复: restore_session 超时(%ss)，本次自动恢复跳过，插件继续加载', restore_timeout_seconds)
        except asyncio.CancelledError:
            self._mark_auto_recover_result('启动恢复跳过', '启动阶段任务被取消，restore_session 跳过')
            logger.warning('[vrc_friend_radar] 启动恢复: start阶段收到取消信号，跳过 restore_session，插件继续加载')
        except Exception as exc:
            self._mark_auto_recover_result('启动恢复异常', str(exc))
            logger.warning('[vrc_friend_radar] 启动恢复: restore_session 发生异常，按失败处理并继续加载。err=%s', exc)

        if restored:
            self._last_health_check_at = time.time()
            self._last_session_persist_at = time.time()
        else:
            stored = self.session_store.load() or {}
            has_stored_session = bool(str(stored.get('username', '') or '').strip() and str(stored.get('cookie', '') or '').strip())
            if has_stored_session:
                logger.info('[vrc_friend_radar] 启动恢复: 已尝试基于 session.json(username+cookie) 恢复，失败后不再使用本地密码自动重登，请管理员手动 /vrc登录')
            else:
                logger.info('[vrc_friend_radar] 启动恢复: 无可用 session(username+cookie)，等待管理员手动登录')
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self.client.is_logged_in():
            try:
                self.persist_session(force=True)
            except Exception as exc:
                logger.warning('[vrc_friend_radar] 停止前持久化会话失败: %s', exc)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._pending_logins.clear()
        self.client.close()

    async def _run_loop(self) -> None:
        while self._running:
            self._tick_count += 1
            self._cleanup_pending_logins()
            if self.client.is_logged_in():
                await self._try_periodic_session_persist()
                await self._try_periodic_health_check()
            if self.cfg.allow_auto_push and self.client.is_logged_in():
                _loop_now = time.time()
                if _loop_now < self._post_login_cooldown_until:
                    _remaining = int(self._post_login_cooldown_until - _loop_now)
                    logger.info('[vrc_friend_radar] 登录后冷却期内，跳过本轮自动检测，剩余 %ss', _remaining)
                else:
                    try:
                        if self._poll_lock.locked():
                            logger.info('[vrc_friend_radar] 本轮自动检测跳过：检测/同步任务仍在执行中（互斥锁占用）')
                        else:
                            detect_timeout = max(10, min(self.cfg.poll_interval_seconds - 5, 120))
                            events = await asyncio.wait_for(self.detect_changes(), timeout=detect_timeout)
                            if events and self._event_callback:
                                try:
                                    await self._event_callback(events)
                                except Exception as exc:
                                    if isinstance(exc, VRChatAuthInvalidError) or self.client.is_auth_invalid_exception(exc):
                                        logger.warning(f"[vrc_friend_radar] 事件回调阶段遇到认证失效: {exc}")
                                        self._record_disconnect_reason('auth invalid', str(exc), source='event_callback')
                                        recovered = await self.auto_recover_login(str(exc), trigger_exc=exc, source='event_callback')
                                        if recovered:
                                            logger.info('[vrc_friend_radar] 事件回调认证失效后自动恢复成功，将在下一轮继续监控')
                                    else:
                                        logger.error(f"[vrc_friend_radar] 事件回调执行失败: {exc}", exc_info=True)
                    except asyncio.TimeoutError:
                        logger.error("[vrc_friend_radar] 轮询检测超时，已跳过本轮")
                    except VRChatAuthInvalidError as exc:
                        logger.warning(f"[vrc_friend_radar] 轮询检测遇到认证失效: {exc}")
                        self._record_disconnect_reason('auth invalid', str(exc), source='detect_changes')
                        recovered = await self.auto_recover_login(str(exc), trigger_exc=exc, source='detect_changes_auth_invalid')
                        if recovered:
                            logger.info('[vrc_friend_radar] 认证失效后自动恢复成功，将在下一轮继续监控')
                    except VRChatClientError as exc:
                        if self.client.is_auth_invalid_exception(exc):
                            logger.warning(f"[vrc_friend_radar] 轮询检测遇到疑似认证失效(VRChatClientError): {exc}")
                            self._record_disconnect_reason('auth invalid', str(exc), source='detect_changes_client_error')
                            recovered = await self.auto_recover_login(str(exc), trigger_exc=exc, source='detect_changes_client_error')
                            if recovered:
                                logger.info('[vrc_friend_radar] 疑似认证失效后自动恢复成功，将在下一轮继续监控')
                        else:
                            logger.error(f"[vrc_friend_radar] 轮询检测失败: {exc}", exc_info=True)
                    except Exception as exc:
                        if self.client.is_auth_invalid_exception(exc):
                            logger.warning(f"[vrc_friend_radar] 轮询检测遇到疑似认证失效(unknown): {exc}")
                            self._record_disconnect_reason('auth invalid', str(exc), source='detect_changes_unknown')
                            recovered = await self.auto_recover_login(str(exc), trigger_exc=exc, source='detect_changes_unknown_error')
                            if recovered:
                                logger.info('[vrc_friend_radar] 疑似认证失效后自动恢复成功，将在下一轮继续监控')
                        else:
                            logger.error(f"[vrc_friend_radar] 轮询检测失败: {exc}", exc_info=True)
            if self._loop_tick_callback:
                try:
                    await self._loop_tick_callback(datetime.now())
                except Exception as exc:
                    logger.error(f"[vrc_friend_radar] 轮询Tick回调执行失败: {exc}", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=max(1, self.cfg.poll_interval_seconds))
                break
            except asyncio.TimeoutError:
                continue

    async def test_login(
        self,
        username: str,
        password: str,
        two_factor_code: str | None = None,
        attempt_id: str | None = None,
    ) -> LoginResult:
        temp_client = VRChatClient(self.cfg.vrchat_user_agent)
        attempt_key = str(attempt_id or '').strip()
        adopted_client = False
        try:
            result = await temp_client.login(
                username=username,
                password=password,
                two_factor_code=two_factor_code,
            )
            if attempt_key and attempt_key not in self._manual_login_attempts:
                logger.warning(
                    '[vrc_friend_radar] 手动登录任务已完成，但等待已被放弃；本次结果将被丢弃，不写入当前会话。attempt_id=%s',
                    attempt_key,
                )
                temp_client.close()
                raise asyncio.CancelledError('manual login attempt abandoned')

            self._adopt_logged_in_client(temp_client)
            adopted_client = True
            self._last_login_result = result
            self.persist_session(force=True)

            if self.cfg.watch_self:
                await self._safe_fetch_self_snapshot(
                    stage='manual_login_post_auth',
                    trigger_recover_if_auth_invalid=False,
                )

            self._post_login_cooldown_until = time.time() + 60
            self._last_health_check_at = time.time()
            self._reset_auto_recover_2fa_waiting()
            logger.info('[vrc_friend_radar] 登录成功，已设置 60 秒冷却期及健康检查计时重置')
            return result
        except Exception:
            if not adopted_client:
                temp_client.close()
            raise
        finally:
            if attempt_key:
                self._manual_login_attempts.discard(attempt_key)

    async def sync_friends(self) -> list[FriendSnapshot]:
        async with self._poll_lock:
            # 信息浏览类能力（好友列表/搜索/在线列表）保持全好友范围
            snapshots = await self.client.fetch_friend_snapshots(None)
            self.db.upsert_friend_snapshots(snapshots)
            self._last_sync_count = len(snapshots)

            await self._safe_fetch_self_snapshot(stage='sync_friends_post_fetch', trigger_recover_if_auth_invalid=False)

            self._persist_session_if_cookie_changed()
            return snapshots

    def get_sync_debug(self) -> dict[str, int]:
        return self.client.get_last_sync_debug()

    async def _resolve_self_context(self) -> tuple[str, FriendSnapshot | None]:
        """
        解析本轮监控所需的本人信息。
        - watch_self=False: 直接跳过；
        - watch_self=True 且 current_user_id 为空: 主动请求当前用户快照以刷新 user_id；
        返回：(self_user_id, self_snapshot)
        """
        if not self.cfg.watch_self:
            return '', None

        self_id = (self.client.get_current_user_id() or '').strip()
        self_snapshot: FriendSnapshot | None = None
        if self_id:
            return self_id, None

        # 关键兜底：某些恢复登录/重启场景下 current_user_id 可能为空，主动拉取一次本人快照
        self_snapshot = await self._safe_fetch_self_snapshot(
            stage='detect_resolve_self_context',
            trigger_recover_if_auth_invalid=True,
        )
        if self_snapshot is not None:
            self_id = (self_snapshot.friend_user_id or '').strip()

        if not self_id:
            logger.warning('[vrc_friend_radar] watch_self=true 但当前无法获取 current_user_id，本轮仅监控普通好友')
        else:
            logger.info('[vrc_friend_radar] watch_self=true 且 current_user_id 缺失，已通过 fetch_self_snapshot() 刷新')
        return self_id, self_snapshot

    async def detect_changes(self) -> list[RadarEvent]:
        async with self._poll_lock:
            base_watch_ids = self._dedupe_clean_ids(self.get_effective_watch_friends())
            resolved_self_id, self_snapshot = await self._resolve_self_context()

            watch_ids = base_watch_ids
            if resolved_self_id:
                watch_ids = self._dedupe_clean_ids([*watch_ids, resolved_self_id])

            if not watch_ids:
                logger.info("[vrc_friend_radar] 本轮变化检测跳过：监控名单为空")
                self._last_sync_count = 0
                self._last_detected_events = []
                return []

            watch_set = set(watch_ids)
            old_map_all = self.db.get_friend_snapshot_map()
            old_map = {friend_id: old_map_all[friend_id] for friend_id in watch_ids if friend_id in old_map_all}
            new_snapshots = await self.client.fetch_friend_snapshots(watch_ids)

            if resolved_self_id and resolved_self_id in watch_set:
                if self_snapshot is None:
                    self_snapshot = await self._safe_fetch_self_snapshot(
                        stage='detect_merge_self_snapshot',
                        trigger_recover_if_auth_invalid=False,
                    )
                if self_snapshot is not None and (self_snapshot.friend_user_id or '').strip():
                    # 以当前登录账号实时快照为准，覆盖同ID项（理论上好友列表不应包含自己）
                    merged = {item.friend_user_id: item for item in new_snapshots}
                    merged[self_snapshot.friend_user_id] = self_snapshot
                    new_snapshots = list(merged.values())

            current_self_id = resolved_self_id or (self.client.get_current_user_id() or '').strip()
            new_snapshot_map = {item.friend_user_id: item for item in new_snapshots}
            synth_updated_at = datetime.now().isoformat(timespec='seconds')
            for friend_id, old_item in old_map.items():
                if friend_id in new_snapshot_map:
                    continue
                if current_self_id and friend_id == current_self_id:
                    continue
                old_status = str(old_item.status or '').strip().lower()
                old_location = str(old_item.location or '').strip().lower()
                if old_status == 'offline' and old_location in {'', 'offline'}:
                    continue
                synthetic = FriendSnapshot(
                    friend_user_id=friend_id,
                    display_name=old_item.display_name or friend_id,
                    status='offline',
                    location='offline',
                    status_description=old_item.status_description,
                    updated_at=synth_updated_at,
                )
                new_snapshots.append(synthetic)
                new_snapshot_map[friend_id] = synthetic
                logger.warning(
                    '[vrc_friend_radar] watched friend missing from API response, synthesized offline snapshot: friend_id=%s old_status=%s old_location=%s',
                    friend_id,
                    old_item.status,
                    old_item.location,
                )

            new_snapshots = [item for item in new_snapshots if item.friend_user_id in watch_set]

            new_map = {item.friend_user_id: item for item in new_snapshots}
            if self.cfg.watch_self:
                if not current_self_id:
                    logger.warning('[vrc_friend_radar] coroom self context missing: watch_self=true but current_self_id is empty in this detect round')
                elif current_self_id not in new_map:
                    logger.warning('[vrc_friend_radar] coroom self snapshot missing in merged snapshots: self_id=%s, watch_count=%s, snapshot_count=%s', current_self_id, len(watch_ids), len(new_snapshots))
            if current_self_id and current_self_id in new_map:
                self_item = new_map[current_self_id]
                self._mark_self_snapshot_success(self_item, stage='detect_changes_from_snapshot_map')

            raw_events: list[RadarEvent] = []
            for item in new_snapshots:
                old_item = old_map.get(item.friend_user_id)
                if old_item is None:
                    continue
                raw_events.extend(diff_snapshot(old_item, item))

            filtered_events: list[RadarEvent] = []
            skipped_status_events = 0
            skipped_world_events = 0
            for event in raw_events:
                if event.event_type in {"friend_online", "friend_offline", "status_changed", "status_message_changed"}:
                    if not self.cfg.enable_status_tracking:
                        skipped_status_events += 1
                        continue
                    if (
                        current_self_id
                        and event.friend_user_id == current_self_id
                        and event.event_type in {"friend_online", "friend_offline", "status_changed"}
                    ):
                        old_status = str(event.old_value or '').strip().lower()
                        new_status = str(event.new_value or '').strip().lower()
                        if {old_status, new_status}.issubset({'offline', 'web_online'}):
                            skipped_status_events += 1
                            logger.info(
                                "[vrc_friend_radar] self status web-presence transition suppressed: old=%s, new=%s",
                                event.old_value,
                                event.new_value,
                            )
                            continue
                if event.event_type == "location_changed":
                    if not self.cfg.enable_world_tracking:
                        skipped_world_events += 1
                        continue
                    if current_self_id and event.friend_user_id == current_self_id and not self._should_track_self_location_change(event.old_value, event.new_value):
                        skipped_world_events += 1
                        continue
                filtered_events.append(event)

            events = self._dedupe_events(filtered_events)
            self.db.upsert_friend_snapshots(new_snapshots)
            coroom_events = self._build_coroom_events(new_snapshots)
            if coroom_events:
                coroom_events = self._filter_coroom_events_by_interval(coroom_events)
                events.extend(coroom_events)
            logger.info(
                "[vrc_friend_radar] 本轮变化检测完成: status_tracking=%s, world_tracking=%s, raw_events=%s, filtered_events=%s, deduped_events=%s, coroom_events=%s, skipped_status=%s, skipped_world=%s",
                self.cfg.enable_status_tracking,
                self.cfg.enable_world_tracking,
                len(raw_events),
                len(filtered_events),
                len(events),
                len(coroom_events),
                skipped_status_events,
                skipped_world_events,
            )
            self.db.insert_event_history(events)
            self._last_sync_count = len(new_snapshots)
            self._last_detected_events = events
            self._persist_session_if_cookie_changed()
            return events

    def _build_coroom_events(self, snapshots: list[FriendSnapshot]) -> list[RadarEvent]:
        now = datetime.now().isoformat(timespec='seconds')
        allow = set(self.get_monitor_watch_friend_ids())
        if not allow:
            logger.info('[vrc_friend_radar] coroom skipped: empty watch list')
            return []

        current_self_id = (self.client.get_current_user_id() or '').strip() if self.cfg.watch_self else ''
        self_present_in_round = bool(current_self_id and any(item.friend_user_id == current_self_id for item in snapshots))
        if self.cfg.watch_self and current_self_id and not self_present_in_round:
            logger.warning('[vrc_friend_radar] coroom self missing in this round: self_id=%s, snapshots=%s', current_self_id, len(snapshots))

        grouped: dict[str, list[FriendSnapshot]] = {}
        for item in snapshots:
            if item.friend_user_id not in allow:
                continue
            status_text = (item.status or '').strip().lower()
            if status_text == 'offline':
                continue
            location_key = get_location_group_key(item.location)
            if not location_key:
                logger.info(
                    '[vrc_friend_radar] coroom skip snapshot: user=%s name=%s status=%s location=%s reason=location_not_groupable',
                    item.friend_user_id,
                    item.display_name,
                    item.status,
                    item.location,
                )
                continue
            grouped.setdefault(location_key, []).append(item)

        if grouped:
            for location_key in sorted(grouped.keys()):
                logger.info(
                    '[vrc_friend_radar] coroom grouped: key=%s members=%s ids=%s',
                    location_key,
                    len(grouped[location_key]),
                    '|'.join(sorted(i.friend_user_id for i in grouped[location_key])),
                )
        else:
            logger.info('[vrc_friend_radar] coroom grouped: no valid location groups in this round')

        events: list[RadarEvent] = []
        active_location_keys: list[str] = []
        min_members = self.cfg.coroom_notify_min_members
        joinable_only = self.cfg.coroom_notify_joinable_only

        for location_key, members in grouped.items():
            member_count = len(members)
            if member_count < min_members:
                extra_reason = ''
                if self.cfg.watch_self and (not current_self_id or not self_present_in_round):
                    extra_reason = ' (possible self missing)'
                logger.info(
                    '[vrc_friend_radar] coroom group filtered: key=%s members=%s reason=min_members(%s)%s',
                    location_key,
                    member_count,
                    min_members,
                    extra_reason,
                )
                continue

            joinability = infer_joinability(location_key)
            if joinable_only and joinability != '可加入':
                logger.info(
                    '[vrc_friend_radar] coroom group filtered: key=%s members=%s reason=joinable_only joinability=%s',
                    location_key,
                    member_count,
                    joinability,
                )
                continue

            members.sort(key=lambda x: x.friend_user_id)
            signature = '|'.join(item.friend_user_id for item in members)
            old_signature = self.db.get_coroom_signature(location_key)
            self.db.set_coroom_signature(location_key, signature, now)
            active_location_keys.append(location_key)
            if old_signature == signature:
                logger.info('[vrc_friend_radar] coroom group deduped by signature: key=%s signature=%s', location_key, signature)
                continue

            display_names = '、'.join(sorted(item.display_name for item in members))
            events.append(
                RadarEvent(
                    friend_user_id=location_key,
                    display_name=display_names,
                    event_type='co_room',
                    old_value=old_signature,
                    new_value=signature,
                    created_at=now,
                )
            )
            logger.info(
                '[vrc_friend_radar] coroom event built: key=%s members=%s old_signature=%s new_signature=%s',
                location_key,
                member_count,
                old_signature,
                signature,
            )

        self.db.delete_coroom_state_except(active_location_keys)
        return events

    def _filter_coroom_events_by_interval(self, events: list[RadarEvent]) -> list[RadarEvent]:
        now_ts = time.time()
        min_interval = self.cfg.coroom_notify_interval_seconds
        result: list[RadarEvent] = []
        active_keys = set()
        for event in events:
            location_key = event.friend_user_id
            active_keys.add(location_key)
            last_ts = self._last_coroom_notify_at.get(location_key, 0.0)
            elapsed = now_ts - last_ts
            if elapsed < min_interval:
                logger.info(
                    '[vrc_friend_radar] coroom group filtered: key=%s reason=throttle elapsed=%.1fs min_interval=%ss',
                    location_key,
                    elapsed,
                    min_interval,
                )
                continue
            self._last_coroom_notify_at[location_key] = now_ts
            result.append(event)
        stale_keys = [
            key
            for key, ts in self._last_coroom_notify_at.items()
            if key not in active_keys and (now_ts - ts) > min_interval
        ]
        for key in stale_keys:
            self._last_coroom_notify_at.pop(key, None)
        return result

    def _dedupe_events(self, events: list[RadarEvent]) -> list[RadarEvent]:
        lower_bound = (datetime.now() - timedelta(seconds=self.cfg.event_dedupe_window_seconds)).isoformat(timespec='seconds')
        seen_in_batch: set[str] = set()
        result: list[RadarEvent] = []
        for event in events:
            dedupe_key = f"{event.friend_user_id}:{event.event_type}:{event.old_value}:{event.new_value}"
            if dedupe_key in seen_in_batch:
                continue
            if self.db.event_exists_since(dedupe_key, lower_bound):
                continue
            seen_in_batch.add(dedupe_key)
            result.append(event)
        return result

    def build_event_messages(self, events: list[RadarEvent]) -> list[str]:
        messages = [self.notifier.build_message(event) for event in events]
        return messages[: self.cfg.event_batch_size]

    def list_cached_friends(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        return self.db.list_friend_snapshots(limit=limit, offset=offset)

    def list_online_cached_friends(self, limit: int = 20, offset: int = 0) -> list[FriendSnapshot]:
        return self.db.list_online_friend_snapshots(limit=limit, offset=offset)

    def list_coroom_groups(self, apply_query_filters: bool = True) -> list[dict]:
        watch_ids = self.get_monitor_watch_friend_ids()
        if not watch_ids:
            return []
        min_members = self.cfg.coroom_notify_min_members if apply_query_filters else 2
        return self.db.list_coroom_groups(watch_ids, min_members=min_members)

    def count_online_cached_friends(self) -> int:
        return self.db.count_online_friend_snapshots()

    def count_cached_friends(self) -> int:
        return self.db.count_friend_snapshots()

    def list_recent_events(self, limit: int = 20) -> list[RadarEvent]:
        watch_ids = self.get_monitor_watch_friend_ids()
        if not watch_ids:
            return []

        watch_set = set(watch_ids)
        raw_events = self.db.list_recent_events(limit=max(limit * 10, 100))
        filtered: list[RadarEvent] = []
        for item in raw_events:
            if item.event_type == 'co_room':
                member_ids = [x for x in (item.new_value or '').split('|') if x]
                # 同房事件要求成员全部位于监控名单，避免历史非监控数据泄露
                if member_ids and all(member_id in watch_set for member_id in member_ids):
                    filtered.append(item)
            else:
                if item.friend_user_id in watch_set:
                    filtered.append(item)
            if len(filtered) >= limit:
                break
        return filtered

    def create_pending_login(self, session_key: str, username: str, password: str, method: str) -> None:
        self._pending_logins[session_key] = PendingLoginSession(session_key=session_key, username=username, password=password, created_at=time.time(), method=method)

    def get_pending_login(self, session_key: str) -> PendingLoginSession | None:
        self._cleanup_pending_logins()
        return self._pending_logins.get(session_key)

    def pop_pending_login(self, session_key: str) -> PendingLoginSession | None:
        self._cleanup_pending_logins()
        return self._pending_logins.pop(session_key, None)

    def has_pending_login(self, session_key: str) -> bool:
        self._cleanup_pending_logins()
        return session_key in self._pending_logins

    def _cleanup_pending_logins(self) -> None:
        expired_keys = [key for key, session in self._pending_logins.items() if session.is_expired(self.cfg.login_session_timeout_seconds)]
        if not expired_keys:
            return
        for key in expired_keys:
            self._pending_logins.pop(key, None)
        if '__auto_recover__' in expired_keys and self._is_waiting_2fa_for_auto_recover:
            self._is_waiting_2fa_for_auto_recover = False
            self._auto_recover_pending_method = ''
            self._mark_auto_recover_result('2FA超时', '自动恢复等待2FA超时，已退出等待状态')
            logger.warning('[vrc_friend_radar] 自动恢复等待2FA会话已超时，恢复自动重试能力')


    def _record_self_snapshot_failure(self, stage: str, exc: Exception | None = None, fallback: str = '') -> tuple[str, str]:
        category, detail = self._classify_failure_reason(exc, fallback=fallback)
        if category in {'auth invalid', 'invalid credentials'}:
            category = 'self fetch unauthorized'
        detail_text = detail or fallback or 'unknown'
        self._last_self_snapshot_failure_reason = f"{category}: {detail_text}"
        self._last_self_snapshot_failure_stage = (stage or '').strip() or 'unknown'
        self._last_self_snapshot_failure_at = time.time()
        self._self_snapshot_failure_streak += 1
        logger.warning(
            '[vrc_friend_radar] self snapshot失败 stage=%s category=%s streak=%s detail=%s',
            self._last_self_snapshot_failure_stage,
            category,
            self._self_snapshot_failure_streak,
            detail_text,
        )
        return category, detail_text

    def _mark_self_snapshot_success(self, snapshot: FriendSnapshot, stage: str = '') -> None:
        self._last_self_presence_status = str(snapshot.status or '')
        self._last_self_presence_location = str(snapshot.location or '')
        self._last_self_presence_updated_at = str(snapshot.updated_at or datetime.now().isoformat(timespec='seconds'))
        self._last_self_snapshot_success_at = time.time()
        if self._self_snapshot_failure_streak > 0:
            logger.info(
                '[vrc_friend_radar] self snapshot恢复成功 stage=%s reset_failure_streak=%s',
                stage or 'unknown',
                self._self_snapshot_failure_streak,
            )
        self._self_snapshot_failure_streak = 0

    async def _safe_fetch_self_snapshot(self, stage: str, trigger_recover_if_auth_invalid: bool = False) -> FriendSnapshot | None:
        try:
            snapshot = await self.client.fetch_self_snapshot()
        except Exception as exc:
            category, detail = self._record_self_snapshot_failure(stage=stage, exc=exc)
            if category == 'self fetch unauthorized' and trigger_recover_if_auth_invalid:
                await self.auto_recover_login(
                    f'self snapshot unauthorized at {stage}: {detail}',
                    trigger_exc=exc,
                    source=f'self_snapshot_{stage}',
                )
            return None

        if snapshot is None:
            self._record_self_snapshot_failure(stage=stage, fallback='fetch_self_snapshot returned None')
            return None

        self._mark_self_snapshot_success(snapshot, stage=stage)
        return snapshot

    def _format_self_snapshot_failure_text(self) -> str:
        if self._last_self_snapshot_failure_at <= 0:
            return '无'
        fail_time = datetime.fromtimestamp(self._last_self_snapshot_failure_at).isoformat(timespec='seconds')
        fail_reason = self._last_self_snapshot_failure_reason or 'unknown'
        fail_stage = self._last_self_snapshot_failure_stage or 'unknown'
        success_time = (
            datetime.fromtimestamp(self._last_self_snapshot_success_at).isoformat(timespec='seconds')
            if self._last_self_snapshot_success_at > 0
            else '无'
        )
        return (
            f"{fail_time} | stage={fail_stage} | {fail_reason} | "
            f"连续失败={self._self_snapshot_failure_streak} | 最近成功={success_time}"
        )

    def _format_self_presence_text(self) -> str:
        status = (self._last_self_presence_status or '').strip().lower()
        location = (self._last_self_presence_location or '').strip()
        updated_at = (self._last_self_presence_updated_at or '').strip()
        if not status and not location:
            if self.client.is_logged_in():
                return '未获取到本人在线快照（会话已登录，待下一轮同步）'
            return '未登录/会话不可用（尚未获取到当前账号在线快照）'
        if status == 'offline':
            state = '离线/仅Web在线'
        elif status:
            state = f'在线({status})'
        else:
            state = '在线状态未知'
        if location:
            return f"{state} | location={location} | updated_at={updated_at or 'unknown'}"
        return f"{state} | updated_at={updated_at or 'unknown'}"

    def get_runtime_summary(self) -> str:
        self._cleanup_pending_logins()
        login_text = "未测试"
        if self._last_login_result is not None:
            login_text = f"成功({self._last_login_result.display_name})" if self._last_login_result.ok else f"失败({self._last_login_result.message})"
        auto_recover = self.get_auto_recover_status()
        auto_recover_time = auto_recover.get('last_time') or '无'
        auto_recover_result = auto_recover.get('last_result') or '未知'
        auto_recover_reason = auto_recover.get('last_reason') or '无'
        auto_recover_waiting_2fa = '是' if auto_recover.get('waiting_2fa') else '否'
        auto_recover_waiting_method = auto_recover.get('waiting_2fa_method') or '无'
        auto_recover_attempts = auto_recover.get('attempts_in_window', 0)
        auto_recover_max = auto_recover.get('max_attempts', self._auto_recover_max_attempts)
        auto_recover_window = auto_recover.get('window_seconds', self._auto_recover_window_seconds)
        last_disconnect_time = datetime.fromtimestamp(self._last_disconnect_at).isoformat(timespec='seconds') if self._last_disconnect_at > 0 else '无'
        last_disconnect_category = self._last_disconnect_reason_category or '无'
        last_disconnect_detail = self._last_disconnect_reason_detail or '无'

        return (
            "VRChat好友雷达运行中\n"
            f"轮询间隔: {self.cfg.poll_interval_seconds} 秒\n"
            f"监控好友数: {len(self.get_monitor_watch_friend_ids())}\n"
            f"通知群数: {len(self.get_effective_notify_groups())}\n"
            f"缓存好友数: {self.count_cached_friends()}\n"
            f"等待验证码会话数: {len(self._pending_logins)}\n"
            f"最近登录测试: {login_text}\n"
            f"最近同步好友数: {self._last_sync_count}\n"
            f"最近变化事件数: {len(self._last_detected_events)}\n"
            f"自动恢复最近时间: {auto_recover_time}\n"
            f"自动恢复最近结果: {auto_recover_result}\n"
            f"自动恢复失败/触发原因: {auto_recover_reason}\n"
            f"自动恢复待2FA: {auto_recover_waiting_2fa} ({auto_recover_waiting_method})\n"
            f"自动恢复尝试计数: {auto_recover_attempts}/{auto_recover_max} ({auto_recover_window}秒窗口)\n"
            f"最近掉线时间: {last_disconnect_time}\n"
            f"最近掉线分类: {last_disconnect_category}\n"
            f"最近掉线详情: {last_disconnect_detail}\n"
            f"去重窗口: {self.cfg.event_dedupe_window_seconds} 秒\n"
            f"同房提醒间隔: {self.cfg.coroom_notify_interval_seconds} 秒\n"
            f"同房提醒人数阈值: {self.cfg.coroom_notify_min_members} 人\n"
            f"同房仅提醒可加入: {'开启' if self.cfg.coroom_notify_joinable_only else '关闭'}\n"
            f"每日任务默认时间: {self.cfg.daily_task_time}\n"
            f"日报推送: {'开启' if self.cfg.enable_daily_report else '关闭'} ({self.cfg.daily_report_time})\n"
            f"单次推送上限: {self.cfg.event_batch_size} 条\n"
            f"自动推送: {'开启' if self.cfg.allow_auto_push else '关闭'}\n"
            f"Web/API登录态: {'已登录' if self.client.is_logged_in() else '未登录'}\n"
            f"当前账号客户端在线态: {self._format_self_presence_text()}\n"
            f"self snapshot最近失败: {self._format_self_snapshot_failure_text()}\n"
            f"轮询次数: {self._tick_count}"
        )
