import json
from pathlib import Path
from astrbot.api import logger


class SessionStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.base_dir / "session.json"

    def save(self, data: dict) -> None:
        self.file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> dict | None:
        if not self.file_path.exists():
            return None
        try:
            return json.loads(self.file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 读取 session.json 失败: {exc}", exc_info=True)
            return None

    def clear(self) -> None:
        if self.file_path.exists():
            self.file_path.unlink(missing_ok=True)
