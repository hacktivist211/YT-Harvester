"""Dynamic quality & format execution matrix."""

from __future__ import annotations

from .matrix import (
    CODEC_PRIORITY_DEFAULT,
    FormatMatrix,
    audio_family,
    codec_family,
    codec_label,
    is_audio_only,
    is_hdr,
    is_progressive,
    is_video_only,
    video_height,
)
from .resolver import (
    Candidate,
    ResolvedManifest,
    StreamManifestResolver,
    build_selector,
    combined_selector,
)
from .planner import MediaPlan, plan_audio, plan_for, plan_video

__all__ = [
    "CODEC_PRIORITY_DEFAULT",
    "FormatMatrix",
    "audio_family",
    "codec_family",
    "codec_label",
    "is_audio_only",
    "is_hdr",
    "is_progressive",
    "is_video_only",
    "video_height",
    "Candidate",
    "ResolvedManifest",
    "StreamManifestResolver",
    "build_selector",
    "combined_selector",
    "MediaPlan",
    "plan_audio",
    "plan_for",
    "plan_video",
]
