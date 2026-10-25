"""FFmpeg + tagging post-processing."""

from __future__ import annotations

from .ffmpeg import FFmpegRunner
from .tagging import (
    AudioTagger,
    dump_metadata_json,
    guess_mime,
    lyrics_from_info,
    tag_audio_file,
)

__all__ = [
    "FFmpegRunner",
    "AudioTagger",
    "dump_metadata_json",
    "guess_mime",
    "lyrics_from_info",
    "tag_audio_file",
]
