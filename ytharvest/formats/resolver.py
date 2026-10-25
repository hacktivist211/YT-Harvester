"""Stream manifest resolver + pre-calculated fallback decision tree.

The resolver splits the adaptive manifest into **video-only** and
**audio-only** constituents, applies the :class:`FormatMatrix` and returns an
ordered fallback chain::

    Target Stream
      -> Next best codec @ target resolution
      -> Lower resolution @ best codec
      -> Combined legacy stream (progressive / muxed)

Every rung is an independent yt-dlp selector, so a failure at rung *n* simply
promotes rung *n+1* without failing the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..errors import FormatSelectionError, ManifestError
from .matrix import (
    FormatMatrix,
    is_audio_only,
    is_progressive,
    is_video_only,
    video_height,
)

MAX_CHAIN = 14
LEGACY_RESERVE = 4   # slots always kept for the legacy backstops


@dataclass
class Candidate:
    rung: int
    kind: str                 # target | alt_codec | lower_res | legacy
    label: str
    selector: str
    video: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None

    @property
    def video_height(self) -> int:
        return video_height(self.video) if self.video else 0

    @property
    def codec(self) -> str:
        if not self.video:
            return "n/a"
        return str(self.video.get("vcodec") or "n/a")

    def describe(self) -> str:
        from .matrix import codec_label

        if self.video is None:
            return f"rung{self.rung} {self.kind}: {self.label}"
        return (
            f"rung{self.rung} {self.kind}: {codec_label(self.video.get('vcodec'))} "
            f"{self.video_height}p -> {self.selector}"
        )


@dataclass
class ResolvedManifest:
    info: dict[str, Any]
    video_only: list[dict[str, Any]] = field(default_factory=list)
    audio_only: list[dict[str, Any]] = field(default_factory=list)
    progressive: list[dict[str, Any]] = field(default_factory=list)
    target_height: int | None = None
    chain: list[Candidate] = field(default_factory=list)

    def selectors(self) -> list[str]:
        return [c.selector for c in self.chain]

    def summary(self) -> list[str]:
        return [c.describe() for c in self.chain]


class StreamManifestResolver:
    def __init__(self, matrix: FormatMatrix | None = None) -> None:
        self.matrix = matrix or FormatMatrix()

    @classmethod
    def from_rules(cls, rules: Any) -> "StreamManifestResolver":
        return cls(FormatMatrix.from_rules(rules))

    # ------------------------------------------------------------ splitting
    @staticmethod
    def split(info: dict[str, Any]) -> tuple[list[dict], list[dict], list[dict]]:
        formats = info.get("formats")
        if formats is None:
            raise ManifestError("Manifest carries no 'formats' array")
        video, audio, progressive = [], [], []
        for fmt in formats:
            if not isinstance(fmt, dict):  # pragma: no cover - defensive
                continue
            if fmt.get("format_id") in (None, "sb0", "storyboard"):  # skip thumbnails
                continue
            if is_video_only(fmt):
                video.append(fmt)
            elif is_audio_only(fmt):
                audio.append(fmt)
            elif is_progressive(fmt):
                progressive.append(fmt)
        return video, audio, progressive

    # ------------------------------------------------------- target height
    def resolve_target_height(self, video_formats: Iterable[dict[str, Any]]) -> int | None:
        heights = [
            video_height(f)
            for f in video_formats
            if video_height(f) and self.matrix.allowed(f)
        ]
        if not heights:
            return None
        target = max(heights)
        if self.matrix.max_height is not None:
            target = min(target, self.matrix.max_height)
        return target

    # ------------------------------------------------------------ audio pick
    def best_audio_for(self, video: dict[str, Any] | None, audios: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not audios:
            return None
        ordered = self.matrix.sorted_audio(audios)
        if video is None:
            return ordered[0]
        # keep container families happy: mp4 video -> m4a audio, webm -> opus
        want = "mp4a" if str(video.get("ext")) == "mp4" else "opus"
        for fmt in ordered:
            if want in str(fmt.get("acodec") or "").lower():
                return fmt
        return ordered[0]

    # ------------------------------------------------------------ chain
    def build_chain(self, info: dict[str, Any]) -> ResolvedManifest:
        video_only, audio_only, progressive = self.split(info)
        target = self.resolve_target_height(video_only)
        resolved = ResolvedManifest(
            info=info,
            video_only=video_only,
            audio_only=audio_only,
            progressive=progressive,
            target_height=target,
        )

        chain: list[Candidate] = []
        seen: set[str] = set()

        def add(rung: int, kind: str, label: str, video: dict | None, audio: dict | None,
                 selector: str | None = None) -> None:
            sel = selector or build_selector(video, audio)
            if not sel or sel in seen:
                return
            seen.add(sel)
            chain.append(
                Candidate(rung=rung, kind=kind, label=label, selector=sel, video=video, audio=audio)
            )

        eligible = [f for f in video_only if self.matrix.allowed(f)]
        codecs = list(self.matrix.codec_priority)
        known_codecs = []
        for codec in codecs:
            if any(str(f.get("vcodec") or "").startswith(codec) for f in eligible):
                known_codecs.append(codec)

        # ---- rung 1: target stream (best codec @ target resolution)
        if target is not None:
            for codec in known_codecs:
                at_target = [
                    f
                    for f in eligible
                    if str(f.get("vcodec") or "").startswith(codec) and video_height(f) == target
                ]
                if not at_target:
                    continue
                best = self.matrix.sorted_videos(at_target, target)[0]
                audio = self.best_audio_for(best, audio_only)
                add(1, "target", f"{codec} @ {target}p", best, audio)
                break

        # ---- rung 2: next best codec @ target resolution
        if target is not None:
            for codec in known_codecs:
                at_target = [
                    f
                    for f in eligible
                    if str(f.get("vcodec") or "").startswith(codec) and video_height(f) == target
                ]
                if not at_target:
                    continue
                best = self.matrix.sorted_videos(at_target, target)[0]
                audio = self.best_audio_for(best, audio_only)
                add(2, "alt_codec", f"next codec {codec} @ {target}p", best, audio)

        # ---- rung 3: lower resolution @ best codec (never crowds out rung 4)
        if target is not None:
            lower_heights = sorted(
                {video_height(f) for f in eligible if video_height(f) < target}, reverse=True
            )
            for height in lower_heights:
                if len(chain) >= MAX_CHAIN - LEGACY_RESERVE:
                    break
                for codec in known_codecs:
                    at_height = [
                        f
                        for f in eligible
                        if str(f.get("vcodec") or "").startswith(codec) and video_height(f) == height
                    ]
                    if not at_height:
                        continue
                    best = self.matrix.sorted_videos(at_height, height)[0]
                    audio = self.best_audio_for(best, audio_only)
                    add(3, "lower_res", f"lower res {codec} @ {height}p", best, audio)

        # ---- rung 4: combined legacy stream (muxed formats / generic selector)
        if progressive:
            best_prog = self.matrix.sorted_videos(progressive, target)[0]
            add(4, "legacy", "combined legacy (progressive)", best_prog, None)
        if audio_only:
            best_audio = self.matrix.sorted_audio(audio_only)[0]
            add(4, "legacy", "audio-only fallback", None, best_audio)
        if video_only or audio_only or progressive:
            add(4, "legacy", "generic best", None, None, selector="bv*+ba/b")

        if not chain:
            raise FormatSelectionError(
                "No usable format candidates found in the manifest "
                f"(video-only={len(video_only)}, audio-only={len(audio_only)}, progressive={len(progressive)})"
            )
        resolved.chain = chain[:MAX_CHAIN]
        return resolved

    def resolve(self, info: dict[str, Any]) -> ResolvedManifest:
        return self.build_chain(info)

    # ------------------------------------------------------- audio-only tree
    def build_audio_chain(self, info: dict[str, Any]) -> ResolvedManifest:
        """Audio mode: pure audio-only constituents with a legacy backstop.

        Rung 1: best source stream for the requested codec (highest bitrate)
        Rung 2: next best source codec
        Rung 3: remaining sources, descending quality
        Rung 4: combined legacy ``bestaudio/best`` selector
        """
        video, audio, progressive = self.split(info)
        resolved = ResolvedManifest(
            info=info, video_only=video, audio_only=audio, progressive=progressive,
            target_height=None,
        )
        if not audio and not progressive:
            raise FormatSelectionError(
                f"Manifest carries no audio constituents (audio={len(audio)}, "
                f"progressive={len(progressive)})"
            )
        chain: list[Candidate] = []
        seen: set[str] = set()

        ordered = self.matrix.sorted_audio(audio)
        for index, fmt in enumerate(ordered):
            rung = 1 if index == 0 else (2 if index == 1 else 3)
            kind = "target" if rung == 1 else ("alt_codec" if rung == 2 else "lower_res")
            selector = build_selector(None, fmt)
            if selector in seen:
                continue
            seen.add(selector)
            chain.append(
                Candidate(
                    rung=rung,
                    kind=kind,
                    label=f"audio {fmt.get('acodec')} @{fmt.get('tbr', '?')}kbps",
                    selector=selector,
                    video=None,
                    audio=fmt,
                )
            )
        if progressive:
            best = self.matrix.sorted_audio(progressive)[0]
            selector = build_selector(best, None)
            if selector not in seen:
                seen.add(selector)
                chain.append(
                    Candidate(rung=4, kind="legacy", label="combined legacy (progressive)",
                              selector=selector, video=best, audio=None)
                )
        chain.append(
            Candidate(rung=4, kind="legacy", label="generic best audio", selector="bestaudio/best")
        )
        resolved.chain = chain[:MAX_CHAIN]
        return resolved

    def resolve_audio(self, info: dict[str, Any]) -> ResolvedManifest:
        return self.build_audio_chain(info)


def build_selector(video: dict[str, Any] | None, audio: dict[str, Any] | None) -> str:
    vid = str(video.get("format_id")) if video else ""
    aid = str(audio.get("format_id")) if audio else ""
    if vid and aid:
        return f"{vid}+{aid}"
    if vid:
        return vid
    if aid:
        return aid
    return ""


def combined_selector(chain: Iterable[Candidate]) -> str:
    """Collapse the whole tree into one yt-dlp selector with internal fallback."""
    parts = [c.selector for c in chain]
    return "/".join(dict.fromkeys(parts))
