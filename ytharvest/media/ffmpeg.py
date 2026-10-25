"""FFmpeg / FFprobe driver for copy, remux and transcode paths."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from ..errors import MediaError
from ..formats.planner import MediaPlan


class FFmpegRunner:
    def __init__(self, binary: str = "ffmpeg", probe: str = "ffprobe", timeout: int = 1800) -> None:
        self.binary = binary
        self.probe_bin = probe
        self.timeout = timeout
        self.last_stderr: str = ""

    # ----------------------------------------------------------- availability
    def available(self) -> bool:
        return shutil.which(self.binary) is not None and shutil.which(self.probe_bin) is not None

    def require(self) -> None:
        if not self.available():
            raise MediaError(
                f"FFmpeg not found ('{self.binary}'/'{self.probe_bin}'). Install it and retry."
            )

    # ----------------------------------------------------------------- probe
    def probe(self, path: str | Path) -> dict[str, Any]:
        self.require()
        cmd = [
            self.probe_bin,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=min(self.timeout, 120)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
            raise MediaError(f"ffprobe failed: {exc}") from exc
        if out.returncode != 0:
            raise MediaError(f"ffprobe error: {(out.stderr or '').strip()[:300]}")
        try:
            return json.loads(out.stdout or "{}")
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise MediaError("ffprobe returned malformed JSON") from exc

    def container_ok(self, path: str | Path) -> bool:
        """Probe-based integrity check used by staging validation."""
        try:
            data = self.probe(path)
        except MediaError:
            return False
        streams = data.get("streams") or []
        if not streams:
            return False
        fmt = data.get("format") or {}
        try:
            return float(fmt.get("duration") or 0) > 0
        except (TypeError, ValueError):  # pragma: no cover
            return False

    def duration(self, path: str | Path) -> float:
        try:
            fmt = self.probe(path).get("format") or {}
            return float(fmt.get("duration") or 0.0)
        except (MediaError, TypeError, ValueError):  # pragma: no cover
            return 0.0

    # ------------------------------------------------------------------- run
    def _run(self, cmd: Sequence[str]) -> None:
        try:
            out = subprocess.run(
                list(cmd), capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"FFmpeg timed out after {self.timeout}s") from exc
        except OSError as exc:  # pragma: no cover
            raise MediaError(f"Cannot execute FFmpeg: {exc}") from exc
        self.last_stderr = (out.stderr or "")[-4000:]
        if out.returncode != 0:
            tail = [line for line in (out.stderr or "").splitlines() if line.strip()][-6:]
            raise MediaError("FFmpeg failed: " + " | ".join(tail))

    # -------------------------------------------------------------- actions
    def remux(self, src: str | Path, dst: str | Path, extra_args: Sequence[str] = ()) -> Path:
        self.require()
        dst = Path(dst)
        cmd = [self.binary, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        cmd += list(extra_args) or ["-c", "copy"]
        cmd += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", str(dst)]
        self._run(cmd)
        if not dst.exists() or dst.stat().st_size == 0:
            raise MediaError(f"FFmpeg produced no output at {dst}")
        return dst

    def transcode_audio(self, src: str | Path, dst: str | Path, plan: MediaPlan,
                        start: float | None = None, end: float | None = None) -> Path:
        self.require()
        dst = Path(dst)
        cmd = [self.binary, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        if start is not None:
            cmd += ["-ss", f"{start:.3f}"]
        if end is not None:
            cmd += ["-to", f"{end:.3f}"]
        cmd += ["-vn", "-map", "0:a:0"]
        cmd += list(plan.ffmpeg_args)
        cmd += [str(dst)]
        self._run(cmd)
        if not dst.exists() or dst.stat().st_size == 0:
            raise MediaError(f"FFmpeg produced no output at {dst}")
        return dst

    def merge(self, video: str | Path, audio: str | Path, dst: str | Path,
              extra_args: Sequence[str] = ()) -> Path:
        self.require()
        dst = Path(dst)
        cmd = [
            self.binary, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
        ]
        cmd += list(extra_args) or ["-c", "copy"]
        cmd += ["-map", "0:v:0", "-map", "1:a:0", str(dst)]
        self._run(cmd)
        if not dst.exists() or dst.stat().st_size == 0:
            raise MediaError(f"FFmpeg produced no output at {dst}")
        return dst

    def extract_audio(self, src: str | Path, dst: str | Path, plan: MediaPlan) -> Path:
        return self.transcode_audio(src, dst, plan)

    def version(self) -> str:
        try:
            out = subprocess.run([self.binary, "-version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
            return "unavailable"
        first = (out.stdout or "").splitlines()
        return first[0] if first else "unavailable"
