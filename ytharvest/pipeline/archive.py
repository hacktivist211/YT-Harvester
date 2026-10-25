"""yt-dlp download-archive wrapper (archive-on-resume).

The archive is a plain ``extractor video_id`` line file. Entries are only
written *after* the atomic move succeeded, so re-running a partially completed
batch never re-downloads finished work.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable


class DownloadArchive:
    def __init__(self, path: str | Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        if self.enabled and self.path.exists():
            self._seen = {line.strip() for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()}

    @staticmethod
    def line_for(extractor: str | None, video_id: str | None) -> str | None:
        if not video_id:
            return None
        # yt-dlp archive lines use the lowercase extractor id ("youtube xyz")
        return f"{(extractor or 'youtube').split(':')[0].lower()} {video_id}"

    def contains(self, extractor: str | None, video_id: str | None) -> bool:
        line = self.line_for(extractor, video_id)
        return bool(line) and line in self._seen

    def add(self, extractor: str | None, video_id: str | None) -> bool:
        line = self.line_for(extractor, video_id)
        if not line or line in self._seen:
            return False
        with self._lock:
            self._seen.add(line)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True

    def add_many(self, pairs: Iterable[tuple[str | None, str | None]]) -> int:
        return sum(1 for extractor, vid in pairs if self.add(extractor, vid))

    def read(self) -> list[str]:
        if not self.path.exists():
            return []
        return [line.strip() for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def __len__(self) -> int:
        return len(self._seen)

    def as_ydl_option(self) -> str | None:
        return str(self.path) if self.enabled else None
