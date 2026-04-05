import json
from pathlib import Path
from astrbot.api import logger


class SessionStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.base_dir / "session.json"

    @staticmethod
    def _sanitize_session_data(data: dict | None) -> dict:
        payload = data or {}
        username = str(payload.get('username', '') or '').strip()
        cookie = str(payload.get('cookie', '') or '').strip()
        sanitized: dict[str, str] = {}
        if username:
            sanitized['username'] = username
        if cookie:
            sanitized['cookie'] = cookie
        return sanitized

    def save(self, data: dict) -> None:
        try:
            sanitized = self._sanitize_session_data(data)
            payload = json.dumps(sanitized, ensure_ascii=False, indent=2)
            tmp_path = self.file_path.with_suffix('.json.tmp')
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self.file_path)
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 写入 session.json 失败: {exc}", exc_info=True)

    def load(self) -> dict | None:
        if not self.file_path.exists():
            return None
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("[vrc_friend_radar] session.json 格式异常（非对象），已忽略")
                return None

            has_legacy_password = 'password' in data and str(data.get('password', '') or '').strip() != ''
            sanitized = self._sanitize_session_data(data)
            if has_legacy_password:
                logger.warning("[vrc_friend_radar] 检测到旧版 session.json 含明文 password，已在内存中忽略并自动清理落盘")
                # 自动迁移：去掉 password 字段，降低明文口令落盘风险
                self.save(sanitized)
            return sanitized
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 读取 session.json 失败: {exc}", exc_info=True)
            return None

    def clear(self) -> None:
        try:
            if self.file_path.exists():
                self.file_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 删除 session.json 失败: {exc}", exc_info=True)
