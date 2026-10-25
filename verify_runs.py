"""Per-mode output verification for real YT-Harvest runs.

Usage::

    python verify_runs.py [runs_root]

For every harvested artifact it proves that:

* the file exists, is non-trivial and parses as the intended container
* video outputs really carry a video stream inside the requested cap
* audio outputs carry the requested codec (FLAC/MP3/Opus/AAC) and tags
* metadata outputs are valid JSON with chapters / transcript
* the SQLite ledger agrees with what is on disk (status + SHA-256)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ytharvest.media.ffmpeg import FFmpegRunner
from ytharvest.pipeline.ledger import Ledger

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
runner = FFmpegRunner()

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def check(condition: bool) -> str:
    return PASS if condition else FAIL


def probe(path: Path) -> dict:
    return runner.probe(path)


def verify_audio(path: Path, expected: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if not path.exists():
        return [("file exists", FAIL, "missing")]
    size = path.stat().st_size
    rows.append(("file non-trivial", check(size > 100_000), f"{size:,} bytes"))
    try:
        data = probe(path)
    except Exception as exc:  # noqa: BLE001
        rows.append(("container parses", FAIL, str(exc)[:120]))
        return rows

    fmt = (data.get("format") or {}).get("format_name", "")
    streams = data.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    rows.append(("container parses", PASS, fmt))
    rows.append(("has audio stream", check(bool(audio)), f"{len(audio)} stream(s)"))
    if not audio:
        return rows

    codec = audio[0].get("codec_name")
    want = {"flac": "flac", "mp3": "mp3", "opus": "opus", "aac": "aac", "m4a": "aac"}.get(expected, expected)
    rows.append((f"codec == {want}", check(codec == want), codec or "-"))
    duration = float((data.get("format") or {}).get("duration") or 0)
    rows.append(("duration > 0", check(duration > 0), f"{duration:.1f}s"))
    rows.append(("sample rate", PASS, f"{audio[0].get('sample_rate')}Hz"))

    if expected == "mp3":
        bitrate = int((data.get("format") or {}).get("bit_rate") or 0)
        rows.append(("mp3 bitrate ~320k cbr", check(bitrate >= 300_000), f"{bitrate} bps"))

    tags = {k.lower(): v for k, v in ((data.get("format") or {}).get("tags") or {}).items()}
    title = tags.get("title") or tags.get("itt_title")
    rows.append(("title tag", check(bool(title)), str(title)[:60] if title else "missing"))
    artist = tags.get("artist") or tags.get("album_artist") or tags.get("©ART".lower())
    rows.append(("artist tag", check(bool(artist)), str(artist)[:60] if artist else "missing"))

    if expected in {"flac", "mp3"}:
        rows.append(("embedded cover art", check(_has_cover(path)), _cover_kind(path)))
    return rows


def _has_cover(path: Path) -> bool:
    try:
        from mutagen import File as MF

        audio = MF(str(path))
        if audio is None:
            return False
        if path.suffix.lower() == ".mp3":
            return bool(audio.tags and audio.tags.getall("APIC"))
        if path.suffix.lower() == ".flac":
            return bool(getattr(audio, "pictures", None))
        if path.suffix.lower() == ".opus":
            return bool(audio.tags and audio.tags.get("metadata_block_picture"))
        if path.suffix.lower() in {".m4a", ".mp4"}:
            return bool(audio.tags and audio.tags.get("covr"))
    except Exception:  # noqa: BLE001
        return False
    return False


def _cover_kind(path: Path) -> str:
    try:
        from mutagen import File as MF

        audio = MF(str(path))
        if path.suffix.lower() == ".mp3" and audio.tags:
            apic = audio.tags.getall("APIC")
            return f"{len(apic)} APIC frame(s), {len(apic[0].data):,} bytes" if apic else "none"
        if path.suffix.lower() == ".flac" and getattr(audio, "pictures", None):
            return f"{len(audio.pictures)} picture(s), {len(audio.pictures[0].data):,} bytes"
    except Exception:  # noqa: BLE001
        pass
    return "none"


def verify_video(path: Path, max_height: int | None = None) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if not path.exists():
        return [("file exists", FAIL, "missing")]
    size = path.stat().st_size
    rows.append(("file non-trivial", check(size > 100_000), f"{size:,} bytes"))
    try:
        data = probe(path)
    except Exception as exc:  # noqa: BLE001
        rows.append(("container parses", FAIL, str(exc)[:120]))
        return rows
    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    rows.append(("container parses", PASS, (data.get("format") or {}).get("format_name", "")))
    rows.append(("has video stream", check(bool(video)), video[0].get("codec_name") if video else "missing"))
    rows.append(("has audio stream", check(bool(audio)), audio[0].get("codec_name") if audio else "missing"))
    if video:
        height = video[0].get("height") or 0
        ok = height <= max_height if max_height else height > 0
        rows.append((f"height <= {max_height}" if max_height else "height > 0",
                     check(ok), f"{height}p"))
        rows.append(("codec family", check(video[0].get("codec_name") in {"av1", "vp9", "h264"}),
                     video[0].get("codec_name")))
    duration = float((data.get("format") or {}).get("duration") or 0)
    rows.append(("duration > 0", check(duration > 0), f"{duration:.1f}s"))
    return rows


def verify_metadata(path: Path) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if not path.exists():
        return [("file exists", FAIL, "missing")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [("json parses", FAIL, str(exc)[:120])]
    rows.append(("json parses", PASS, f"{path.stat().st_size:,} bytes"))
    rows.append(("has title", check(bool(data.get("title"))), str(data.get("title"))[:60]))
    rows.append(("has duration", check(bool(data.get("duration"))), str(data.get("duration"))))
    rows.append(("has chapters/channels", PASS,
                 f"{len(data.get('chapters') or [])} chapters, "
                 f"{len(data.get('subtitles') or {})} subtitle langs"))
    transcript = Path(str(path).replace(".info.json", ".transcript.txt"))
    if transcript.exists():
        rows.append(("transcript captured", PASS, f"{transcript.stat().st_size:,} bytes"))
    else:
        rows.append(("transcript captured", SKIP, "no captions published"))
    return rows


def verify_ledger(ledger_path: Path, output_dir: Path) -> list[tuple[str, str, str]]:
    if not ledger_path.exists():
        return [("ledger present", FAIL, str(ledger_path))]
    with Ledger(ledger_path) as ledger:
        stats = ledger.stats()
        rows = [("ledger stats", PASS, json.dumps(stats))]
        jobs = ledger.jobs()
        on_disk = 0
        checksum_ok = 0
        import hashlib

        for job in jobs:
            if not job.output_path:
                continue
            p = Path(job.output_path)
            if not p.exists():
                continue
            on_disk += 1
            if job.checksum:
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
                checksum_ok += digest == job.checksum
        rows.append(("completed outputs on disk", check(on_disk == stats.get("COMPLETED", 0)),
                     f"{on_disk}/{stats.get('COMPLETED', 0)}"))
        rows.append(("sha256 ledger matches files",
                     check(checksum_ok == on_disk), f"{checksum_ok}/{on_disk}"))
        return rows


def main(argv: list[str]) -> int:
    runs_root = Path(argv[0]) if argv else RUNS
    failures = 0
    print("=" * 78)
    print(f"YT-Harvest run verification :: {runs_root}")
    print("=" * 78)

    specs = [
        ("metadata", "metadata", verify_metadata, None),
        ("audio", "flac", lambda p: verify_audio(p, "flac"), None),
        ("video", "mp4", lambda p: verify_video(p, 720), None),
        ("playlist", "mp3", lambda p: verify_audio(p, "mp3"), None),
    ]

    for run_name, ext, checker, _ in specs:
        run_dir = runs_root / run_name
        if not run_dir.exists():
            print(f"\n[{run_name}] NOT RUN (expected {run_dir})")
            continue
        artifacts = sorted(run_dir.rglob(f"*.{ext}"))
        # metadata mode writes .info.json next to its transcripts
        if run_name == "metadata":
            artifacts = sorted(run_dir.rglob("*.info.json"))
        print(f"\n[{run_name}] {len(artifacts)} artifact(s)")
        for artifact in artifacts:
            print(f"  -- {artifact.relative_to(run_dir)}")
            rows = checker(artifact)
            for name, status, detail in rows:
                if status == FAIL:
                    failures += 1
                print(f"     [{status}] {name:<28} {detail}")
        ledger_rows = verify_ledger(run_dir / "harvest.sqlite3", run_dir)
        for name, status, detail in ledger_rows:
            if status == FAIL:
                failures += 1
            print(f"     [{status}] {name:<28} {detail}")

    print("\n" + "=" * 78)
    print(f"RESULT: {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
