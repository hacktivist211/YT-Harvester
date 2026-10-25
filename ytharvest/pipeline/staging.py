"""Staging storage: .part chunks, SHA-256 validation and the atomic move.

Nothing ever appears in the target directory until it has passed validation,
which is what makes a killed process safe to resume.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import StagingError

CHUNK = 1024 * 1024
PART_SUFFIX = ".part"


def sha256_file(path: str | Path, chunk_size: int = CHUNK) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except FileNotFoundError:
        return 0


@dataclass
class ValidationResult:
    ok: bool
    path: Path
    bytes: int
    sha256: str
    reason: str = ""


class StagingArea:
    """Isolated scratch directory in front of the final output directory."""

    def __init__(self, root: str | Path, final_dir: str | Path) -> None:
        self.root = Path(root)
        self.final_dir = Path(final_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.final_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ staging
    def stage_path(self, name: str) -> Path:
        """A .part path inside the staging root (never the final directory)."""
        safe = _sanitize(name)
        if not safe.endswith(PART_SUFFIX):
            safe += PART_SUFFIX
        path = self.root / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def intermediate(self, name: str, suffix: str) -> Path:
        """A non-final path for FFmpeg output (still inside staging).

        The ``.part`` marker sits *before* the real extension so FFmpeg can
        still infer the target container from the filename.
        """
        safe = _sanitize(name)
        suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
        if suffix and safe.endswith(suffix):
            safe = safe[: -len(suffix)]
        path = self.root / f"{safe}.part{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup(self, *paths: str | Path) -> None:
        for path in paths:
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
            except OSError:  # pragma: no cover
                pass

    def purge(self) -> int:
        """Remove every stale chunk (used by ``--clean-staging``)."""
        removed = 0
        if not self.root.exists():
            return 0
        for child in sorted(self.root.rglob("*"), reverse=True):
            try:
                if child.is_file():
                    child.unlink()
                    removed += 1
                elif child.is_dir():
                    child.rmdir()
            except OSError:  # pragma: no cover
                continue
        return removed

    # --------------------------------------------------------- validation
    def validate(
        self,
        path: str | Path,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        min_bytes: int = 1,
        probe: Callable[[Path], bool] | None = None,
    ) -> ValidationResult:
        path = Path(path)
        if not path.exists():
            return ValidationResult(False, path, 0, "", "missing file")
        size = file_size(path)
        if size < min_bytes:
            return ValidationResult(False, path, size, "", f"file too small ({size} bytes)")
        if expected_size and size != expected_size:
            return ValidationResult(
                False, path, size, "", f"size mismatch: expected {expected_size}, got {size}"
            )
        digest = sha256_file(path)
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            return ValidationResult(False, path, size, digest, "sha256 mismatch")
        if probe is not None and not probe(path):
            return ValidationResult(False, path, size, digest, "container probe failed")
        return ValidationResult(True, path, size, digest, "ok")

    # -------------------------------------------------------- atomic move
    def atomic_move(self, src: str | Path, dest_name: str | Path) -> Path:
        """Rename into the target directory only after validation succeeded."""
        src = Path(src)
        if not src.exists():
            raise StagingError(f"Cannot move missing staged file: {src}")
        dest_name = Path(dest_name)
        dest = dest_name if dest_name.is_absolute() else self.final_dir / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # replace() keeps the operation atomic even on Windows
            os.replace(src, dest)
            return dest
        try:
            os.replace(src, dest)
        except OSError:
            # different volumes -> copy then swap
            tmp = dest.with_suffix(dest.suffix + ".tmpmove")
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, dest)
                src.unlink()
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                raise StagingError(f"Atomic move failed: {exc}") from exc
        return dest

    def leftover_parts(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.rglob(f"*{PART_SUFFIX}") if p.is_file())

    def stats(self) -> dict[str, object]:
        parts = self.leftover_parts()
        return {
            "root": str(self.root),
            "chunks": len(parts),
            "bytes": sum(file_size(p) for p in parts),
        }


def _sanitize(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " ._()[]-+#&" else "_" for c in (name or "file"))
    cleaned = cleaned.replace("..", "_")            # no traversal
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        cleaned = "file"
    if len(cleaned) > 180:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 8:
            cleaned = stem[: 180 - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:180]
    return cleaned
