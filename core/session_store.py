import json
from pathlib import Path
from astrbot.api import logger


class SessionStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.base_dir / "session.json"

    def save(self, data: dict) -> None:
        try:
            payload = json.dumps(data or {}, ensure_ascii=False, indent=2)
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
            return data
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 读取 session.json 失败: {exc}", exc_info=True)
            return None

    def clear(self) -> None:
        try:
            if self.file_path.exists():
                self.file_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 删除 session.json 失败: {exc}", exc_info=True)
