import json
from pathlib import Path


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
            self._cache = json.loads(self.file_path.read_text(encoding='utf-8'))
        except Exception:
            self._cache = {}

    def save(self) -> None:
        self.file_path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding='utf-8')

    def get(self, world_id: str) -> dict | None:
        return self._cache.get(world_id)

    def set(self, world_id: str, data: dict) -> None:
        self._cache[world_id] = data
        self.save()
