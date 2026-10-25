"""Metadata, lyrics and cover-art embedding (ID3 / Vorbis / MP4 tag families)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..errors import MediaError

try:  # pragma: no cover - dependency guard
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, USLT
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
except ImportError as exc:  # pragma: no cover
    raise MediaError("mutagen is required for tagging: pip install mutagen") from exc

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def guess_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _picture_block(data: bytes, mime: str) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = mime
    pic.data = data
    return pic


class AudioTagger:
    """Writes title/artist/album/track/lyrics/cover into the right tag family."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise MediaError(f"Cannot tag missing file: {self.path}")
        self.suffix = self.path.suffix.lower()
        self._audio = MutagenFile(str(self.path), easy=True)
        if self._audio is None:
            raise MediaError(f"mutagen cannot open {self.path}")

    # ----------------------------------------------------------- text tags
    def apply_text(self, meta: dict[str, Any]) -> None:
        mapping = {}
        title = meta.get("title")
        artist = meta.get("artist") or meta.get("uploader") or meta.get("channel")
        album = meta.get("album") or meta.get("playlist_title") or meta.get("show")
        track = meta.get("track_number") or meta.get("playlist_index")
        year = meta.get("release_year") or (str(meta.get("upload_date") or "")[:4] or None)
        genre = meta.get("genre")
        if title:
            mapping["title"] = str(title)
        if artist:
            mapping["artist"] = str(artist)
        if album:
            mapping["album"] = str(album)
        if track:
            mapping["tracknumber"] = str(track)
        if year:
            mapping["date"] = str(year)
        if genre:
            mapping["genre"] = str(genre)
        if not mapping:
            return
        try:
            for key, value in mapping.items():
                self._audio[key] = value
            self._audio.save(str(self.path))
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"Tag write failed for {self.path.name}: {exc}") from exc

    # -------------------------------------------------------------- lyrics
    def apply_lyrics(self, lyrics: str) -> None:
        if not lyrics:
            return
        try:
            if self.suffix == ".mp3":
                tags = ID3(str(self.path))
                tags.add(
                    USLT(encoding=3, lang="eng", desc="", text=lyrics)
                )
                tags.save(v2_version=3)
            elif self.suffix == ".flac":
                audio = FLAC(str(self.path))
                audio["LYRICS"] = lyrics
                audio.save()
            elif self.suffix == ".opus":
                audio = OggOpus(str(self.path))
                audio["LYRICS"] = lyrics
                audio.save()
            elif self.suffix in {".m4a", ".mp4"}:
                audio = MP4(str(self.path))
                audio["\xa9lyr"] = lyrics
                audio.save()
            else:  # pragma: no cover - exotic targets
                audio = MutagenFile(str(self.path))
                if audio is not None:
                    try:
                        audio["LYRICS"] = lyrics
                        audio.save()
                    except Exception:  # noqa: BLE001
                        return
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"Lyric write failed for {self.path.name}: {exc}") from exc

    # -------------------------------------------------------------- cover
    def apply_cover(self, data: bytes) -> None:
        if not data:
            return
        mime = guess_mime(data)
        try:
            if self.suffix == ".mp3":
                tags = ID3(str(self.path))
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                tags.save(v2_version=3)
            elif self.suffix == ".flac":
                audio = FLAC(str(self.path))
                audio.clear_pictures()
                audio.add_picture(_picture_block(data, mime))
                audio.save()
            elif self.suffix == ".opus":
                audio = OggOpus(str(self.path))
                block = base64.b64encode(_picture_block(data, mime).write()).decode()
                frames = [f for f in audio.tags.get("metadata_block_picture", [])]
                audio["metadata_block_picture"] = frames + [block]
                audio.save()
            elif self.suffix in {".m4a", ".mp4"}:
                audio = MP4(str(self.path))
                audio["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG if mime == "image/jpeg" else MP4Cover.FORMAT_PNG)]
                audio.save()
            else:  # pragma: no cover
                raise MediaError(f"Cover art unsupported for {self.suffix}")
        except MediaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"Cover write failed for {self.path.name}: {exc}") from exc

    # --------------------------------------------------------------- full
    def apply(
        self,
        meta: dict[str, Any],
        cover: bytes | None = None,
        lyrics: str | None = None,
        cover_enabled: bool = True,
        lyrics_enabled: bool = True,
    ) -> None:
        self.apply_text(meta)
        if lyrics_enabled and lyrics:
            self.apply_lyrics(lyrics)
        if cover_enabled and cover:
            self.apply_cover(cover)

    def read(self) -> dict[str, Any]:
        try:
            return dict(self._audio.tags or {})
        except Exception:  # noqa: BLE001  # pragma: no cover
            return {}


def tag_audio_file(
    path: str | Path,
    meta: dict[str, Any],
    cover: bytes | None = None,
    lyrics: str | None = None,
    cover_enabled: bool = True,
    lyrics_enabled: bool = True,
) -> None:
    AudioTagger(path).apply(
        meta, cover=cover, lyrics=lyrics, cover_enabled=cover_enabled, lyrics_enabled=lyrics_enabled
    )


def lyrics_from_info(info: dict[str, Any]) -> str | None:
    """Best-effort lyric extraction from yt-dlp metadata."""
    for key in ("lyrics", "lyrics_list"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("lyrics")
                    if text:
                        parts.append(str(text))
            if parts:
                return "\n".join(parts)
    return None


def dump_metadata_json(info: dict[str, Any], dest: Path) -> Path:
    payload = {
        key: info.get(key)
        for key in (
            "id", "title", "uploader", "channel", "uploader_id", "channel_id",
            "upload_date", "release_date", "duration", "duration_string",
            "view_count", "like_count", "comment_count", "description",
            "webpage_url", "original_url", "thumbnail", "categories", "tags",
            "language", "chapters", "playlist_title", "playlist_index",
            "subtitles", "automatic_captions", "formats_summary",
        )
        if key in info
    }
    payload["chapters"] = info.get("chapters") or []
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return dest
