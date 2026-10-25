"""Offline end-to-end tests for the execution engine (fake transport + real FFmpeg)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ytharvest.config import FormatRules, GatekeeperRules, HarvestConfig, WorkerRules
from ytharvest.errors import JobFailed
from ytharvest.media.ffmpeg import FFmpegRunner
from ytharvest.pipeline.engine import ExecutionEngine, sanitize_filename, _transcript_from_json3

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required for engine tests",
)


# ------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def tone_webm(tmp_path_factory) -> Path:
    """A real 2-second opus/webm file standing in for a downloaded audio stream."""
    path = tmp_path_factory.mktemp("media") / "tone.webm"
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "libopus", "-b:a", "96k", str(path)],
        capture_output=True, check=True,
    )
    return path


@pytest.fixture(scope="module")
def clip_mp4(tmp_path_factory) -> Path:
    """A real 1-second video+audio mp4 standing in for a merged video download."""
    path = tmp_path_factory.mktemp("media") / "clip.mp4"
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        capture_output=True, check=True,
    )
    return path


class FakeClient:
    """Transport stub: no network, writes a real media file into staging."""

    def __init__(self, tone: Path, clip: Path | None = None, info: dict | None = None,
                 fail_selectors: set[str] | None = None):
        self.tone = tone
        self.clip = clip
        self.fail_selectors = fail_selectors or set()
        self.downloads: list[str] = []
        self.extracts: list[str] = []
        self.base_info = info or {
            "extractor_key": "Youtube",
            "uploader": "Hans Zimmer",
            "formats": [
                {"format_id": "v-av1-1080", "vcodec": "av01.0.08M.08", "acodec": "none",
                 "height": 1080, "fps": 30, "tbr": 5000, "ext": "mp4", "dynamic_range": "SDR"},
                {"format_id": "v-vp9-720", "vcodec": "vp09.00.31.08", "acodec": "none",
                 "height": 720, "fps": 30, "tbr": 2500, "ext": "webm", "dynamic_range": "SDR"},
                {"format_id": "a-opus", "vcodec": "none", "acodec": "opus", "tbr": 160, "ext": "webm"},
                {"format_id": "a-aac", "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 128, "ext": "m4a"},
            ],
        }

    @staticmethod
    def _video_id(url: str) -> str:
        if "v=" in url:
            return url.split("v=", 1)[1].split("&")[0]
        return "vid123"

    def _info_for(self, url: str) -> dict:
        vid = self._video_id(url)
        info = json.loads(json.dumps(self.base_info))
        info["id"] = vid
        info["webpage_url"] = url
        info["title"] = "Test Track" if vid == "vid123" else f"Track {vid}"
        return info

    def extract(self, url: str, purpose: str = "metadata", overrides: dict | None = None, **kw) -> dict:
        self.extracts.append(url)
        return self._info_for(url)

    def download(self, url: str, *, outtmpl: str, format_selector: str, purpose: str = "media",
                 visitor_data: str = "", progress_hooks=(), postprocessor_hooks=(), overrides=None) -> dict:
        self.downloads.append(format_selector)
        if format_selector in self.fail_selectors:
            raise RuntimeError(f"HTTP Error 403: Forbidden for {format_selector}")
        # audio-only selectors yield an audio container, everything else a muxed clip
        audio_only = format_selector.startswith("a-")
        source = self.tone if (audio_only or self.clip is None) else self.clip
        ext = "webm" if source == self.tone else "mp4"
        target = Path(outtmpl.replace("%(ext)s", ext))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return {"filename": str(target)}


def make_config(tmp_path: Path, mode: str = "audio", **kwargs) -> HarvestConfig:
    return HarvestConfig(
        url="https://www.youtube.com/watch?v=vid123",
        mode=mode,
        output_dir=tmp_path / "downloads",
        formats=FormatRules(audio_codec=kwargs.pop("audio_codec", "flac")),
        gatekeeper=GatekeeperRules(vault_path=tmp_path / "session.vault", vault_passphrase="k", seed=1),
        workers=WorkerRules(downloaders=2, assemblers=1, transcoders=1),
        quiet=True,
        **kwargs,
    )


def make_engine(tmp_path: Path, client: FakeClient, mode: str = "audio", **kwargs) -> ExecutionEngine:
    cfg = make_config(tmp_path, mode=mode, **kwargs)
    return ExecutionEngine(cfg, client=client)


def watch(engine: ExecutionEngine, url: str, mode: str) -> list[str]:
    """Chain of fallback rungs recorded in the ledger for a job."""
    job = engine.ledger.get_job(url, mode)
    return list((job.meta or {}).get("chain") or [])


# ------------------------------------------------------------- name sanitizing
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Normal Title", "Normal Title"),
        ('Bad/Name:*?"<>|', "Bad_Name_______"),   # '/' + ':*?"<>|' -> 8 underscores
        ("  spaced   out  ", "spaced out"),
        ("...", "video"),
        ("", "video"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


# -------------------------------------------------------------------- pipeline
def test_audio_pipeline_end_to_end(tmp_path, tone_webm):
    """fetch -> assemble (sha256) -> process (ffmpeg+tags) -> atomic move -> ledger."""
    client = FakeClient(tone_webm)
    engine = make_engine(tmp_path, client, mode="audio", audio_codec="flac")

    result = engine.run_job("https://www.youtube.com/watch?v=vid123", "audio")
    assert result.status == "COMPLETED", result.error
    assert result.output is not None and result.output.exists()
    assert result.output.suffix == ".flac"

    # output lives in the final directory, never in staging
    assert engine.staging.final_dir in result.output.parents
    assert engine.staging.leftover_parts() == []
    assert list(engine.staging.root.glob("*")) == []

    # ledger committed with checksums
    job = engine.ledger.get_job(result.url, "audio")
    assert job.status == "COMPLETED"
    assert job.checksum and len(job.checksum) == 64
    assert job.output_path == str(result.output)
    assert job.attempts >= 1
    assert job.selector                      # the rung actually used was recorded

    # artifacts journaled
    artifacts = engine.ledger.artifacts(job.id)
    assert artifacts[0]["status"] == "FINAL"
    assert artifacts[0]["sha256"] == job.checksum

    # archive-on-resume written only after success
    assert engine.archive.read() == ["youtube vid123"]

    # the file really is FLAC and tagged
    from mutagen.flac import FLAC
    audio = FLAC(result.output)
    assert audio["title"] == ["Test Track"]
    assert audio["artist"] == ["Hans Zimmer"]

    # metadata sidecar next to the media
    sidecar = result.output.with_suffix(".info.json")
    assert sidecar.exists()
    engine.close()


def test_video_pipeline_remuxes_into_the_right_container(tmp_path, tone_webm, clip_mp4):
    client = FakeClient(tone_webm, clip=clip_mp4)
    engine = make_engine(tmp_path, client, mode="video")
    result = engine.run_job("https://www.youtube.com/watch?v=vid123", "video")
    assert result.status == "COMPLETED", result.error
    assert result.output is not None
    assert result.output.suffix == ".mp4"          # AV1 target -> mp4 container

    # the ledger recorded the resolved fallback tree
    chain = watch(engine, result.url, "video")
    assert chain and chain[0].startswith("rung1 target")

    # the file is a valid container with both a video and an audio stream
    runner = FFmpegRunner()
    streams = runner.probe(result.output)["streams"]
    assert {s["codec_type"] for s in streams} >= {"video", "audio"}
    engine.close()


def test_fallback_walks_the_chain_when_the_first_rung_fails(tmp_path, tone_webm):
    """Rung 1 403s -> the engine must transparently drop to the next rung."""
    client = FakeClient(tone_webm, fail_selectors={"a-opus"})
    engine = make_engine(tmp_path, client, mode="audio", audio_codec="opus")

    result = engine.run_job("https://www.youtube.com/watch?v=vid123", "audio")
    assert result.status == "COMPLETED", result.error
    assert client.downloads[0] == "a-opus"               # tried the target stream first
    assert "a-aac" in client.downloads                   # then fell back to the next codec
    assert result.rung == 2

    job = engine.ledger.get_job(result.url, "audio")
    assert job.attempts >= 2
    events = [e["message"] for e in engine.ledger.events(20)]
    assert any("failed" in e for e in events)
    engine.close()


def test_all_rungs_failing_marks_the_job_failed(tmp_path, tone_webm):
    client = FakeClient(tone_webm, fail_selectors={"a-opus", "a-aac", "bestaudio/best"})
    engine = make_engine(tmp_path, client, mode="audio", audio_codec="opus")
    result = engine.run_job("https://www.youtube.com/watch?v=vid123", "audio")
    assert result.status == "FAILED"
    assert result.error and "403" in result.error
    job = engine.ledger.get_job(result.url, "audio")
    assert job.status == "FAILED"
    assert job.attempts >= 3
    engine.close()


def test_resume_skips_completed_job_without_touching_the_network(tmp_path, tone_webm):
    client = FakeClient(tone_webm)
    engine = make_engine(tmp_path, client, mode="audio", audio_codec="opus")
    first = engine.run_job("https://www.youtube.com/watch?v=vid123", "audio")
    assert first.status == "COMPLETED"
    extracts_after_first = len(client.extracts)

    second = engine.run_job("https://www.youtube.com/watch?v=vid123", "audio")
    assert second.status == "COMPLETED"
    assert len(client.extracts) == extracts_after_first     # no re-extraction
    assert len(client.downloads) == 1                       # no re-download
    engine.close()


def test_archive_hit_short_circuits_before_download(tmp_path, tone_webm):
    client = FakeClient(tone_webm)
    engine = make_engine(tmp_path, client, mode="audio", audio_codec="opus")
    engine.archive.add("youtube", "vid123")

    result = engine.run_job("https://www.youtube.com/watch?v=vid123", "audio")
    assert result.status == "SKIPPED"
    assert client.downloads == []
    engine.close()


def test_corrupt_staged_payload_is_rejected_and_refetched(tmp_path, tone_webm):
    client = FakeClient(tone_webm)
    cfg = make_config(tmp_path, mode="audio", audio_codec="opus")
    engine = ExecutionEngine(cfg, client=client)

    url = "https://www.youtube.com/watch?v=vid123"
    engine.fetch(url, "audio")
    staged = Path(engine.ledger.get_job(url, "audio").meta["staged"]["path"])
    staged.write_bytes(b"corrupt")                 # corrupt the chunk between phases

    with pytest.raises(JobFailed):
        engine.assemble(url, "audio")
    job = engine.ledger.get_job(url, "audio")
    assert job.status in {"PENDING", "FAILED"}
    assert "validation failed" in (job.error or "")
    assert not staged.exists()                     # stale chunk dropped
    engine.close()


def test_crash_between_phases_resumes_from_the_ledger(tmp_path, tone_webm):
    """Kill the process after download; a new engine finishes the job."""
    client = FakeClient(tone_webm)
    url = "https://www.youtube.com/watch?v=vid123"

    engine1 = make_engine(tmp_path, client, mode="audio", audio_codec="flac")
    engine1.fetch(url, "audio")
    assert engine1.ledger.get_job(url, "audio").status == "DOWNLOADING"
    engine1.close()                                # "crash" - no assemble/process

    # fresh process: same ledger + staging on disk
    engine2 = make_engine(tmp_path, client, mode="audio", audio_codec="flac")
    job = engine2.ledger.get_job(url, "audio")
    assert job.status == "DOWNLOADING"
    assert job.meta["staged"]["path"]
    engine2.assemble(url, "audio")
    assert engine2.ledger.get_job(url, "audio").status == "PROCESSING"
    engine2.process(url, "audio")
    final = engine2.ledger.get_job(url, "audio")
    assert final.status == "COMPLETED"
    assert Path(final.output_path).exists()
    assert engine2.archive.read() == ["youtube vid123"]
    engine2.close()


def test_batch_runs_three_bounded_queues(tmp_path, tone_webm):
    client = FakeClient(tone_webm)
    cfg = make_config(tmp_path, mode="audio", audio_codec="opus")
    engine = ExecutionEngine(cfg, client=client)

    entries = [
        {"url": f"https://www.youtube.com/watch?v=v{i}", "title": f"Track {i}",
         "playlist_id": "PL1", "playlist_title": "Album", "playlist_index": i}
        for i in (1, 2, 3)
    ]
    results = engine.run_batch(entries, "audio", "Album")
    assert [r.status for r in results] == ["COMPLETED"] * 3
    assert len(engine.ledger.jobs("COMPLETED")) == 3

    album_dir = cfg.output_dir / "Album"
    assert album_dir.exists()
    assert len(list(album_dir.glob("*.opus"))) == 3
    assert engine.ledger.stats()["TOTAL"] == 3
    engine.close()


def test_playlist_urls_are_batched_in_video_mode(tmp_path, tone_webm):
    client = FakeClient(tone_webm)
    cfg = make_config(tmp_path, mode="playlist", audio_codec="opus")
    engine = ExecutionEngine(cfg, client=client)
    entries = [{"url": "https://www.youtube.com/watch?v=vid123", "title": "T",
                "playlist_index": 1}]
    results = engine.run_batch(entries, "video")
    assert results[0].status == "COMPLETED"
    engine.close()


# ------------------------------------------------------------------ metadata
def test_transcript_decoder():
    payload = json.dumps(
        {"events": [{"segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
                    {"segs": [{"utf8": "second line"}]},
                    {"duration": 1000}]}
    ).encode()
    assert _transcript_from_json3(payload) == "Hello world\nsecond line"
    assert _transcript_from_json3(b"not json") is None


def test_metadata_mode_writes_sidecar(tmp_path, tone_webm):
    base = FakeClient(tone_webm).base_info
    base["chapters"] = [{"title": "Intro", "start_time": 0, "end_time": 10}]
    base["subtitles"] = {"en": [{"ext": "json3", "url": "https://example.invalid/cap"}]}
    client = FakeClient(tone_webm, info=base)

    engine = make_engine(tmp_path, client, mode="metadata")
    result = engine.run_metadata("https://www.youtube.com/watch?v=vid123")
    assert result.status == "COMPLETED", result.error
    assert result.output is not None and result.output.name == "Test Track.info.json"
    data = json.loads(result.output.read_text(encoding="utf-8"))
    assert data["title"] == "Test Track"
    assert data["chapters"][0]["title"] == "Intro"
    engine.close()


def test_metadata_mode_resume(tmp_path, tone_webm):
    client = FakeClient(tone_webm)
    engine = make_engine(tmp_path, client, mode="metadata")
    first = engine.run_metadata("https://www.youtube.com/watch?v=vid123")
    assert first.status == "COMPLETED"
    count = len(client.extracts)
    second = engine.run_metadata("https://www.youtube.com/watch?v=vid123")
    assert second.status == "SKIPPED"
    assert len(client.extracts) == count
    engine.close()
