from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class MetadataCache:
    """Small JSON-file cache for metadata query results."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def key_for(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    def path_for_key(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        path = self.path_for_key(key)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def write(self, key: str, payload: dict[str, Any]) -> Path:
        path = self.path_for_key(key)
        tmp_path = path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
        return path

