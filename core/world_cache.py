import json
from pathlib import Path
from astrbot.api import logger


class WorldCache:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.base_dir / "world_cache.json"
        self._cache: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.file_path.exists():
            self._cache = {}
            return
        try:
            data = json.loads(self.file_path.read_text(encoding='utf-8'))
            self._cache = data if isinstance(data, dict) else {}
            if not isinstance(data, dict):
                logger.warning("[vrc_friend_radar] world_cache.json 格式异常（非对象），已重置为空")
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 读取 world_cache.json 失败: {exc}", exc_info=True)
            self._cache = {}

    def save(self) -> None:
        try:
            payload = json.dumps(self._cache, ensure_ascii=False, indent=2)
            tmp_path = self.file_path.with_suffix('.json.tmp')
            tmp_path.write_text(payload, encoding='utf-8')
            tmp_path.replace(self.file_path)
        except Exception as exc:
            logger.error(f"[vrc_friend_radar] 写入 world_cache.json 失败: {exc}", exc_info=True)

    def get(self, world_id: str) -> dict | None:
        return self._cache.get(world_id)

    def set(self, world_id: str, data: dict) -> None:
        key = str(world_id or '').strip()
        if not key:
            return
        self._cache[key] = data or {}
        self.save()
