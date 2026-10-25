"""Staging: .part isolation, SHA-256 validation and the atomic move."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ytharvest.errors import StagingError
from ytharvest.pipeline.staging import StagingArea, file_size, sha256_file


@pytest.fixture()
def area(tmp_path):
    return StagingArea(tmp_path / "staging", tmp_path / "downloads")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_stage_path_is_inside_staging_and_suffixed(area):
    staged = area.stage_path("My Song")
    assert staged.parent == area.root
    assert staged.name.endswith(".part")
    assert area.root in staged.parents
    assert area.final_dir not in staged.parents  # never written to the target dir


def test_stage_path_sanitizes_hostile_names(area):
    staged = area.stage_path("../../etc/passwd:evil*name")
    assert area.root in staged.parents
    assert "/" not in staged.name and ".." not in staged.name


def test_sha256_matches_hashlib(area):
    target = _write(area.root / "blob.bin", b"yt-harvest" * 1000)
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert sha256_file(target) == expected
    assert file_size(target) == len(b"yt-harvest" * 1000)


def test_validate_ok_and_failures(area):
    good = _write(area.root / "good.part", b"x" * 5000)
    result = area.validate(good, expected_size=5000)
    assert result.ok and result.bytes == 5000
    assert len(result.sha256) == 64

    # size mismatch
    bad = area.validate(good, expected_size=4000)
    assert not bad.ok and "size mismatch" in bad.reason

    # checksum mismatch
    bad = area.validate(good, expected_sha256="0" * 64)
    assert not bad.ok and "sha256" in bad.reason

    # too small (truncated download)
    tiny = _write(area.root / "tiny.part", b"xy")
    assert not area.validate(tiny, min_bytes=1024).ok

    # missing
    assert not area.validate(area.root / "nope.part").ok

    # container probe rejection
    reject = area.validate(good, probe=lambda p: False)
    assert not reject.ok and "probe" in reject.reason


def test_atomic_move_lands_in_final_directory(area):
    staged = _write(area.root / "song.part", b"audio-bytes" * 100)
    digest = sha256_file(staged)

    final = area.atomic_move(staged, "song.flac")
    assert final.parent == area.final_dir
    assert final.exists()
    assert not staged.exists()                       # staged copy is gone
    assert sha256_file(final) == digest              # bytes unchanged by the move
    assert list(area.root.glob("*")) == []           # staging is clean


def test_atomic_move_replaces_existing_destination(area):
    _write(area.final_dir / "song.flac", b"old")
    staged = _write(area.root / "song.part", b"new-content")
    final = area.atomic_move(staged, "song.flac")
    assert final.read_bytes() == b"new-content"


def test_atomic_move_refuses_missing_source(area):
    with pytest.raises(StagingError):
        area.atomic_move(area.root / "ghost.part", "ghost.flac")


def test_leftover_parts_and_cleanup(area):
    a = _write(area.root / "a.part", b"1" * 100)
    b = _write(area.root / "nested" / "b.part", b"2" * 100)
    parts = area.leftover_parts()
    assert set(parts) == {a, b}
    area.cleanup(a)
    assert a not in area.leftover_parts()

    stats = area.stats()
    assert stats["chunks"] == 1
    assert stats["bytes"] == 100

    assert area.purge() >= 1
    assert area.leftover_parts() == []


def test_intermediate_names_keep_marker_and_extension(area):
    path = area.intermediate("Title", ".flac")
    assert path.name == "Title.part.flac"     # ffmpeg can infer the container
    assert ".part." in path.name
    assert path.parent == area.root


def test_probe_backed_validation_accepts_real_container(area, tmp_path):
    """The ffmpeg container probe path (used by assemble) accepts valid media."""
    from ytharvest.media.ffmpeg import FFmpegRunner

    runner = FFmpegRunner()
    if not runner.available():  # pragma: no cover
        pytest.skip("ffmpeg not installed")
    media = tmp_path / "tone.wav"
    import subprocess

    subprocess.run(
        [runner.binary, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ar", "44100", str(media)],
        capture_output=True, check=True,
    )
    assert area.validate(media, probe=runner.container_ok).ok
    assert runner.container_ok(media)
    assert not runner.container_ok(_write(tmp_path / "broken.webm", b"not a media file"))
