"""Granular stream selection matrix (codec -> resolution -> fps -> HDR)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

CODEC_PRIORITY_DEFAULT: tuple[str, ...] = ("av01", "vp09", "avc1")

CODEC_LABELS = {
    "av01": "AV1",
    "vp09": "VP9",
    "avc1": "H.264",
    "hev1": "H.265",
    "hvc1": "H.265",
    "the0": "Theora",
    "vp08": "VP8",
}

_VIDEO_ONLY_NOTCH = "video-only"
AUDIO_CODECS = {
    "opus": "opus",
    "mp4a": "aac",
    "aac": "aac",
    "ac-3": "ac3",
    "ec-3": "eac3",
    "flac": "flac",
    "mp3": "mp3",
    "vorbis": "vorbis",
}

_CODEC_RE = re.compile(r"^(av01|vp09|avc1|hev1|hvc1|vp08|the0)", re.I)


def codec_family(codec: str | None) -> str:
    """``av01.0.08M.08...`` -> ``av01``. Returns ``unknown`` for garbage."""
    if not codec or codec in {"none", ""}:
        return ""
    match = _CODEC_RE.match(str(codec).strip())
    return match.group(1).lower() if match else "unknown"


def codec_label(codec: str | None) -> str:
    family = codec_family(codec)
    return CODEC_LABELS.get(family, family or "n/a")


def audio_family(codec: str | None) -> str:
    if not codec or codec == "none":
        return ""
    lowered = str(codec).lower()
    for key in ("opus", "mp4a", "aac", "ac-3", "ec-3", "flac", "mp3", "vorbis"):
        if key in lowered:
            return AUDIO_CODECS[key]
    return "unknown"


def is_hdr(fmt: dict[str, Any]) -> bool:
    dynamic = str(fmt.get("dynamic_range") or "")
    if "HDR" in dynamic.upper():
        return True
    return str(fmt.get("vcodec") or "").lower().startswith(("hev1", "hvc1")) and "10" in dynamic


def video_height(fmt: dict[str, Any]) -> int:
    height = fmt.get("height")
    if height:
        return int(height)
    # fall back to the label dimension, e.g. "1920x1080"
    note = str(fmt.get("format_note") or "")
    match = re.search(r"(\d{3,4})p", note)
    return int(match.group(1)) if match else 0


def video_fps(fmt: dict[str, Any]) -> float:
    try:
        return float(fmt.get("fps") or 0.0)
    except (TypeError, ValueError):  # pragma: no cover
        return 0.0


def is_video_only(fmt: dict[str, Any]) -> bool:
    vcodec = str(fmt.get("vcodec") or "none")
    acodec = str(fmt.get("acodec") or "none")
    return vcodec not in ("none", "") and acodec in ("none", "")


def is_audio_only(fmt: dict[str, Any]) -> bool:
    vcodec = str(fmt.get("vcodec") or "none")
    acodec = str(fmt.get("acodec") or "none")
    return acodec not in ("none", "") and vcodec in ("none", "")


def is_progressive(fmt: dict[str, Any]) -> bool:
    vcodec = str(fmt.get("vcodec") or "none")
    acodec = str(fmt.get("acodec") or "none")
    return vcodec not in ("none", "") and acodec not in ("none", "")


@dataclass(frozen=True)
class VideoRank:
    """Sort key for video-only formats; smaller is better."""

    codec_index: int
    height_deficit: int      # 0 when it matches the target height exactly
    fps_penalty: int
    hdr_penalty: int
    bitrate_penalty: float

    def as_tuple(self) -> tuple[int, int, int, int, float]:
        return (
            self.codec_index,
            self.height_deficit,
            self.fps_penalty,
            self.hdr_penalty,
            self.bitrate_penalty,
        )


@dataclass
class FormatMatrix:
    """Turns user priorities into comparable ranks."""

    codec_priority: Sequence[str] = CODEC_PRIORITY_DEFAULT
    max_height: int | None = None
    min_height: int | None = None
    prefer_fps: int | None = None
    prefer_hdr: bool = False
    audio_codec: str = "opus"
    bitrate_mode: str = "vbr"

    # ------------------------------------------------------------- video
    def codec_index(self, fmt: dict[str, Any]) -> int:
        family = codec_family(fmt.get("vcodec"))
        try:
            return self.codec_priority.index(family)
        except ValueError:
            return len(self.codec_priority) + 1  # unknown codecs rank last

    def rank_video(self, fmt: dict[str, Any], target_height: int | None = None) -> VideoRank:
        height = video_height(fmt)
        if target_height is None:
            deficit = 0
        elif height >= target_height:
            deficit = 0
        else:
            deficit = target_height - height

        fps = video_fps(fmt)
        fps_penalty = 0
        if self.prefer_fps:
            fps_penalty = 0 if fps >= self.prefer_fps else 1

        hdr_penalty = 0
        if self.prefer_hdr:
            hdr_penalty = 0 if is_hdr(fmt) else 1
        else:
            hdr_penalty = 1 if is_hdr(fmt) else 0  # prefer SDR by default

        try:
            tbr = float(fmt.get("tbr") or 0.0)
        except (TypeError, ValueError):  # pragma: no cover
            tbr = 0.0
        return VideoRank(
            codec_index=self.codec_index(fmt),
            height_deficit=deficit,
            fps_penalty=fps_penalty,
            hdr_penalty=hdr_penalty,
            bitrate_penalty=0.0 if tbr else 1.0,
        )

    def sorted_videos(self, formats: Iterable[dict[str, Any]], target_height: int | None = None) -> list[dict[str, Any]]:
        eligible = [f for f in formats if self.allowed(f)] or list(formats)
        return sorted(eligible, key=lambda f: (self.rank_video(f, target_height).as_tuple(), -video_height(f)))

    # ------------------------------------------------------------- audio
    def rank_audio(self, fmt: dict[str, Any]) -> tuple[int, float, int]:
        family = audio_family(fmt.get("acodec"))
        wanted = self.audio_codec
        # for lossless targets the *source* quality matters most
        preference = {
            "opus": ["opus", "aac", "mp3"],
            "aac": ["aac", "mp4a", "opus", "mp3"],
            "mp3": ["mp3", "aac", "opus"],
            "flac": ["opus", "aac", "flac"],
            "wav": ["opus", "aac", "flac"],
        }.get(wanted, ["opus", "aac"])
        try:
            pref = preference.index(family)
        except ValueError:
            pref = len(preference)
        try:
            tbr = float(fmt.get("tbr") or fmt.get("abr") or 0.0)
        except (TypeError, ValueError):  # pragma: no cover
            tbr = 0.0
        abr_penalty = 0 if self.bitrate_mode == "vbr" else int(round(tbr)) % 1
        return (pref, -tbr, abr_penalty)

    def sorted_audio(self, formats: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(formats, key=self.rank_audio)

    # ------------------------------------------------------------ helpers
    def allowed(self, fmt: dict[str, Any]) -> bool:
        height = video_height(fmt)
        if self.max_height is not None and height and height > self.max_height:
            return False
        if self.min_height is not None and height and height < self.min_height:
            return False
        return True

    @classmethod
    def from_rules(cls, rules: Any) -> "FormatMatrix":
        return cls(
            codec_priority=tuple(getattr(rules, "codec_priority", CODEC_PRIORITY_DEFAULT)),
            max_height=getattr(rules, "max_height", None),
            min_height=getattr(rules, "min_height", None),
            prefer_fps=getattr(rules, "prefer_fps", None),
            prefer_hdr=bool(getattr(rules, "prefer_hdr", False)),
            audio_codec=str(getattr(rules, "audio_codec", "opus")),
            bitrate_mode=str(getattr(rules, "bitrate_mode", "vbr")),
        )
