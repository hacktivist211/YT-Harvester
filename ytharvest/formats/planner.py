"""Remux-vs-transcode planner.

Fidelity rules
--------------
* source codec == target container codec  -> **stream copy** (no re-encode, bit exact)
* FLAC / WAV requested                    -> decode best source stream to PCM and
  encode FLAC at maximum compression (level 12), preserving the source sample rate
* MP3 / AAC / Opus mismatch               -> single lossy re-encode at the
  requested bitrate mode (VBR by default, CBR when asked)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import MediaError
from .matrix import audio_family, codec_family

CONTAINER_BY_CODEC = {
    "av01": "mp4",
    "vp09": "mkv",
    "avc1": "mp4",
    "hev1": "mkv",
    "hvc1": "mkv",
}

LOSSLESS_TARGETS = {"flac", "wav"}
LOSSY_TARGETS = {"opus", "aac", "mp3", "m4a"}
SUPPORTED_AUDIO_TARGETS = ("flac", "wav", "opus", "aac", "mp3", "m4a")


@dataclass
class MediaPlan:
    mode: str                       # copy | remux | transcode
    action: str                     # human readable
    source_codec: str
    target: str                     # audio codec or container
    output_ext: str
    ffmpeg_args: list[str] = field(default_factory=list)
    video: bool = False
    container: str | None = None
    reason: str = ""

    @property
    def needs_ffmpeg(self) -> bool:
        return self.mode in {"remux", "transcode"} or bool(self.ffmpeg_args)


def audio_extension(target: str) -> str:
    target = target.lower()
    if target == "m4a":
        return "m4a"
    if target in {"aac"}:
        return "m4a"   # AAC inside ADTS is rarely what users want; mux to m4a
    return target


def _vbr_args(target: str, bitrate_mode: str, bitrate: str) -> list[str]:
    if target == "mp3":
        return ["-c:a", "libmp3lame", "-q:a", "0"] if bitrate_mode == "vbr" else [
            "-c:a", "libmp3lame", "-b:a", bitrate,
        ]
    if target in {"aac", "m4a"}:
        return ["-c:a", "aac", "-b:a", bitrate if bitrate_mode == "cbr" else "256k"]
    if target == "opus":
        args = ["-c:a", "libopus", "-b:a", bitrate if bitrate_mode == "cbr" else "160k"]
        args += ["-vbr", "on"] if bitrate_mode == "vbr" else ["-vbr", "off"]
        return args
    raise MediaError(f"Unsupported lossy audio target: {target}")


def plan_audio(
    audio_fmt: dict[str, Any] | None,
    target: str = "opus",
    bitrate_mode: str = "vbr",
    bitrate: str = "192k",
) -> MediaPlan:
    target = target.lower()
    if target not in SUPPORTED_AUDIO_TARGETS:
        raise MediaError(f"Unsupported audio target: {target}")
    source = audio_family((audio_fmt or {}).get("acodec"))
    ext = audio_extension(target)

    # ---- direct remux: identical codec, zero re-encode ------------------
    if target in {"opus"} and source == "opus":
        return MediaPlan(
            mode="copy",
            action="stream copy (opus -> ogg/webm, bit exact)",
            source_codec=source or "opus",
            target=target,
            output_ext="opus",
            ffmpeg_args=["-c:a", "copy"],
            reason="source codec already matches the requested container",
        )
    if target in {"aac", "m4a"} and source == "aac":
        return MediaPlan(
            mode="copy",
            action="stream copy (aac -> m4a, bit exact)",
            source_codec=source or "aac",
            target=target,
            output_ext="m4a",
            ffmpeg_args=["-c:a", "copy", "-movflags", "+faststart"],
            reason="source codec already matches the requested container",
        )

    # ---- lossless targets ----------------------------------------------
    if target == "flac":
        return MediaPlan(
            mode="transcode",
            action="lossless transcode -> FLAC (compression level 12)",
            source_codec=source,
            target=target,
            output_ext="flac",
            ffmpeg_args=[
                "-c:a", "flac",
                "-compression_level", "12",
                "-map_metadata", "0",
                "-id3v2_version", "3",
            ],
            reason="FLAC requires decoding the source to PCM (sample rate preserved)",
        )
    if target == "wav":
        return MediaPlan(
            mode="transcode",
            action="lossless decode -> WAV PCM",
            source_codec=source,
            target=target,
            output_ext="wav",
            ffmpeg_args=["-c:a", "pcm_s16le", "-map_metadata", "0"],
            reason="WAV stores raw PCM; decode-only step, no quality loss",
        )

    # ---- lossy targets --------------------------------------------------
    return MediaPlan(
        mode="transcode",
        action=f"transcode -> {target.upper()} ({bitrate_mode.upper()})",
        source_codec=source,
        target=target,
        output_ext=ext,
        ffmpeg_args=_vbr_args(target if target != "m4a" else "aac", bitrate_mode, bitrate),
        reason="requested codec differs from the source codec",
    )


def choose_container(video: dict[str, Any] | None) -> str:
    family = codec_family((video or {}).get("vcodec"))
    return CONTAINER_BY_CODEC.get(family, "mp4")


def plan_video(
    video: dict[str, Any] | None,
    audio: dict[str, Any] | None,
    container: str | None = None,
) -> MediaPlan:
    """Video pipeline: always a lossless remux of the selected constituents."""
    container = container or choose_container(video)
    vcodec = codec_family((video or {}).get("vcodec"))
    acodec = audio_family((audio or {}).get("acodec"))
    args = ["-c:v", "copy", "-c:a", "copy"]
    if container == "mp4":
        args += ["-movflags", "+faststart"]
        if vcodec in {"vp09"}:
            args += ["-tag:v", "vp09"]
    return MediaPlan(
        mode="remux",
        action=f"remux video ({vcodec or 'n/a'}) + audio ({acodec or 'n/a'}) -> {container}",
        source_codec=vcodec,
        target=container,
        output_ext=container,
        ffmpeg_args=args,
        video=True,
        container=container,
        reason="video and audio constituents are copied without re-encoding",
    )


def plan_for(
    mode: str,
    video: dict[str, Any] | None,
    audio: dict[str, Any] | None,
    rules: Any,
) -> MediaPlan:
    if mode == "audio":
        return plan_audio(
            audio,
            target=getattr(rules, "audio_codec", "opus"),
            bitrate_mode=getattr(rules, "bitrate_mode", "vbr"),
            bitrate=getattr(rules, "audio_bitrate", "192k"),
        )
    if mode == "video":
        return plan_video(video, audio, getattr(rules, "target_container", None))
    raise MediaError(f"No media plan for mode '{mode}'")
