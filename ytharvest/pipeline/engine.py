"""Transactional execution engine.

Three decoupled phases, each of which can run standalone *or* on its own
bounded async queue:

1. ``fetch``     - metadata extraction + media download into ``.part`` staging
2. ``assemble``  - SHA-256 / size / container validation, plan selection
3. ``process``   - FFmpeg remux/transcode, tagging, atomic move, ledger commit

Every phase persists its progress into the SQLite WAL ledger, so a crash at
any point resumes exactly where it stopped - without re-querying YouTube for
already completed work.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import HarvestConfig
from ..errors import FormatSelectionError, JobFailed, MediaError, ManifestError
from ..formats.planner import MediaPlan, plan_for
from ..formats.resolver import Candidate, StreamManifestResolver
from ..media.ffmpeg import FFmpegRunner
from ..media.tagging import dump_metadata_json, lyrics_from_info, tag_audio_file
from ..net.gatekeeper import EgressGatekeeper
from ..net.ytclient import YouTubeClient
from ..workers.queues import WorkerQueues
from .archive import DownloadArchive
from .ledger import Job, Ledger
from .staging import StagingArea, file_size, sha256_file

VIDEO_FOLDER = "videos"
AUDIO_FOLDER = "audio"
METADATA_FOLDER = "metadata"

STAGED_EXT_OK = {".mp4", ".webm", ".mkv", ".m4a", ".mov", ".ts", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac"}
_FRAGMENT_RE = re.compile(r"\.f\d+\.", re.I)

PROBE_CODEC_MAP = {
    "av1": "av01",
    "vp9": "vp09",
    "vp8": "vp08",
    "h264": "avc1",
    "hevc": "hev1",
    "h265": "hev1",
    "aac": "mp4a",
    "opus": "opus",
    "vorbis": "vorbis",
    "mp3": "mp3",
    "flac": "flac",
    "ac3": "ac-3",
    "eac3": "ec-3",
}


def sanitize_filename(name: str, fallback: str = "video") -> str:
    if not name:
        return fallback
    bad = '\\/:*?"<>|'
    cleaned = "".join("_" if c in bad else c for c in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned or fallback


@dataclass
class JobResult:
    url: str
    mode: str
    status: str
    output: Path | None = None
    selector: str | None = None
    rung: int | None = None
    error: str | None = None
    attempts: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"COMPLETED", "SKIPPED"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "mode": self.mode,
            "status": self.status,
            "output": str(self.output) if self.output else None,
            "selector": self.selector,
            "rung": self.rung,
            "error": self.error,
            "attempts": self.attempts,
            "seconds": round(self.seconds, 2),
            "notes": self.notes,
        }


class ExecutionEngine:
    def __init__(
        self,
        config: HarvestConfig,
        *,
        gatekeeper: EgressGatekeeper | None = None,
        client: YouTubeClient | None = None,
        ledger: Ledger | None = None,
        staging: StagingArea | None = None,
        archive: DownloadArchive | None = None,
        ffmpeg: FFmpegRunner | None = None,
        resolver: StreamManifestResolver | None = None,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        config.ensure_dirs()
        self.report = reporter or (lambda msg: None)
        self.gatekeeper = gatekeeper or EgressGatekeeper(config.gatekeeper)
        if config.cookies_file and config.cookies_file.exists():
            self.gatekeeper.import_cookie_file("imported", config.cookies_file)
        self.client = client or YouTubeClient(self.gatekeeper, quiet=config.quiet)
        self.ledger = ledger or Ledger(config.ledger_path)
        self.staging = staging or StagingArea(config.staging_dir, config.output_dir)
        self.archive = archive or DownloadArchive(config.archive_path, enabled=config.resume)
        self.ffmpeg = ffmpeg or FFmpegRunner()
        self.resolver = resolver or StreamManifestResolver.from_rules(config.formats)
        self._lock = threading.RLock()

    # ================================================================ helpers
    def _folder_for(self, info: dict[str, Any], mode: str) -> Path:
        playlist_title = info.get("playlist_title")
        if playlist_title:
            return self.config.output_dir / sanitize_filename(str(playlist_title))
        base = {"video": VIDEO_FOLDER, "audio": AUDIO_FOLDER, "metadata": METADATA_FOLDER}
        return self.config.output_dir / base.get(mode, "downloads")

    def _meta_of(self, job: Job) -> dict[str, Any]:
        return dict(job.meta or {})

    def run_job(self, url: str, mode: str, hint: dict[str, Any] | None = None) -> JobResult:
        """Sequential single-job execution (fetch -> assemble -> process)."""
        started = time.time()
        if mode == "metadata":
            return self.run_metadata(url, hint)
        try:
            job = self.fetch(url, mode, hint)
            if job.status == "SKIPPED":
                return self._result_from_job(url, mode, started, ["archive"])
            if self.config.resume and job.status == "COMPLETED" and job.output_path \
                    and Path(job.output_path).exists():
                return self._result_from_job(url, mode, started, ["already complete"])
            job = self.assemble(url, mode)
            job = self.process(url, mode)
            return self._result_from_job(url, mode, started, [])
        except Exception as exc:  # noqa: BLE001
            self.gatekeeper.observe(exc, "media")
            return self._fail(url, mode, exc)

    def _fail(self, url: str, mode: str, exc: BaseException, attempts: int = 0) -> JobResult:
        message = f"{type(exc).__name__}: {exc}"
        try:
            job = self.ledger.get_job(url, mode)
            if job:
                self.ledger.mark(url, mode, "FAILED", error=message, bump_attempts=True)
                self.ledger.log(message, level="error", job_id=job.id,
                                data={"traceback": traceback.format_exc()[-1500:]})
                job = self.ledger.get_job(url, mode)
                return JobResult(url, mode, "FAILED", error=message,
                                 attempts=job.attempts if job else attempts)
        except Exception:  # noqa: BLE001 - ledger failure must not mask the cause
            pass
        return JobResult(url, mode, "FAILED", error=message, attempts=attempts)

    def _skip(self, url: str, mode: str, reason: str) -> JobResult:
        job = self.ledger.get_job(url, mode)
        out = Path(job.output_path) if job and job.output_path else None
        if job and job.status != "COMPLETED":
            self.ledger.mark(url, mode, "SKIPPED", error=reason)
        self.report(f"skip [{mode}] {reason}: {url}")
        return JobResult(url, mode, "SKIPPED", output=out, error=reason)

    def _result_from_job(self, url: str, mode: str, started: float, notes: list[str]) -> JobResult:
        job = self.ledger.get_job(url, mode)
        if job is None:  # pragma: no cover
            return JobResult(url, mode, "FAILED", error="job vanished")
        out = Path(job.output_path) if job.output_path else None
        return JobResult(
            url,
            mode,
            job.status,
            output=out,
            selector=job.selector,
            rung=job.rung,
            error=job.error,
            attempts=job.attempts,
            seconds=time.time() - started,
            notes=notes,
        )

    # ================================================================ phases
    def fetch(self, url: str, mode: str, hint: dict[str, Any] | None = None) -> Job:
        """Phase 1: extract the manifest and download media into .part staging."""
        hint = hint or {}
        job = self.ledger.create_job(
            url,
            mode,
            title=hint.get("title"),
            playlist_id=hint.get("playlist_id"),
            playlist_index=hint.get("playlist_index"),
            meta={"hint": {k: hint.get(k) for k in ("title", "playlist_title", "playlist_index")}},
        )
        meta = self._meta_of(job)

        if self.config.resume and job.status == "COMPLETED" and job.output_path:
            if Path(job.output_path).exists():
                return job
            # file disappeared -> redo
            self.ledger.mark(url, mode, "PENDING")

        info = self.client.extract(url, purpose="metadata", overrides={"noplaylist": True})
        visitor = str(info.get("visitor_data") or "")

        # refresh identity against the freshly extracted record
        job = self.ledger.get_job(url, mode) or job
        title = str(info.get("title") or hint.get("title") or job.title or "video")
        self.ledger.mark(
            url,
            mode,
            job.status,
            title=title,
            meta={**meta, "info": _slim_info(info), "title": title},
        )
        meta = self._meta_of(self.ledger.get_job(url, mode))  # type: ignore[assignment]

        extractor = info.get("extractor_key") or "youtube"
        video_id = info.get("id")
        if self.config.resume and self.archive.contains(extractor, video_id):
            self.ledger.mark(url, mode, "SKIPPED", error="already in download archive")
            return self.ledger.get_job(url, mode)  # type: ignore[return-value]

        try:
            manifest = (
                self.resolver.resolve_audio(info) if mode == "audio"
                else self.resolver.build_chain(info)
            )
        except (FormatSelectionError, ManifestError):
            raise
        chain_summary = manifest.summary()
        meta["chain"] = chain_summary
        meta["target_height"] = manifest.target_height
        self.ledger.mark(url, mode, job.status if job.status == "COMPLETED" else "PENDING", meta=meta)
        self.report(f"resolve [{mode}] {title}: {len(manifest.chain)} fallback rungs")

        folder_info = dict(info)
        if not folder_info.get("playlist_title"):
            folder_info["playlist_title"] = (meta.get("hint") or {}).get("playlist_title")
        folder = self._folder_for(folder_info, mode)
        folder.mkdir(parents=True, exist_ok=True)
        stem = sanitize_filename(title)
        outtmpl = str(self.staging.root / f"{stem}.%(ext)s")
        snapshot = {p for p in self.staging.root.rglob("*") if p.is_file()}
        started = time.time()

        chosen: Candidate | None = None
        last_error: str | None = None
        for candidate in manifest.chain:
            self.ledger.mark(
                url, mode, "DOWNLOADING",
                selector=candidate.selector, rung=candidate.rung, bump_attempts=True,
            )
            self.report(
                f"fetch [{mode}] rung {candidate.rung} ({candidate.kind}): {candidate.selector}"
            )
            try:
                self.client.download(
                    url,
                    outtmpl=outtmpl,
                    format_selector=candidate.selector,
                    purpose="media",
                    visitor_data=visitor,
                    progress_hooks=[self._progress_hook(title)],
                    overrides={"noplaylist": True},
                )
            except Exception as exc:  # noqa: BLE001 - fall through the tree
                reaction = self.gatekeeper.observe(exc, "media")
                last_error = f"{type(exc).__name__}: {exc}"
                self.ledger.log(
                    f"rung {candidate.rung} selector {candidate.selector} failed",
                    level="warn",
                    job_id=job.id,
                    data={**reaction, "error": last_error[:500]},
                )
                self.report(f"  ! rung {candidate.rung} failed -> next rung ({last_error[:120]})")
                continue

            produced = self._new_files(snapshot, since=started, stem=stem)
            if not produced:
                last_error = "yt-dlp reported success but produced no file"
                self.ledger.log(last_error, level="warn", job_id=job.id)
                continue
            chosen = candidate
            break

        if chosen is None:
            raise JobFailed(url, last_error or "every fallback rung failed")

        main = self._pick_main(produced, mode)
        meta = self._meta_of(self.ledger.get_job(url, mode))  # type: ignore[assignment]
        meta["staged"] = {
            "path": str(main),
            "bytes": file_size(main),
            "selector": chosen.selector,
            "rung": chosen.rung,
            "kind": chosen.kind,
            "video": chosen.video,
            "audio": chosen.audio,
            "folder": str(folder),
            "stem": stem,
            "title": title,
            "video_id": video_id,
            "extractor": extractor,
            "downloaded_at": time.time(),
            "produced": [str(p) for p in produced],
        }
        self.ledger.mark(url, mode, "DOWNLOADING", meta=meta,
                         bytes_written=file_size(main), selector=chosen.selector, rung=chosen.rung)
        self.ledger.log(f"staged {main.name} ({file_size(main)} bytes)", job_id=job.id)
        return self.ledger.get_job(url, mode)  # type: ignore[return-value]

    def assemble(self, url: str, mode: str) -> Job:
        """Phase 2: validate staged bytes, pick the processing plan."""
        job = self.ledger.get_job(url, mode)
        if job is None:
            raise JobFailed(url, "assemble called before fetch")
        meta = self._meta_of(job)
        staged = meta.get("staged")
        if not staged:
            raise JobFailed(url, "no staged payload recorded in the ledger")

        path = Path(staged["path"])
        probe = self.ffmpeg.container_ok if self.ffmpeg.available() else None
        validation = self.staging.validate(path, min_bytes=1024, expected_size=staged.get("bytes") or None,
                                           probe=probe)
        if not validation.ok:
            # stale/corrupt chunk: drop it so the next attempt refetches
            self.staging.cleanup(path)
            meta.pop("staged", None)
            self.ledger.mark(url, mode, "PENDING", meta=meta,
                             error=f"validation failed: {validation.reason}")
            raise JobFailed(url, f"validation failed: {validation.reason}")

        artifact_id = self.ledger.record_artifact(
            job.id, "staged", path,
            bytes_written=validation.bytes, sha256=validation.sha256, status="VALIDATED",
        )

        # constitution of the actual bytes (fallback rungs may have shifted codecs)
        video_fmt, audio_fmt = staged.get("video"), staged.get("audio")
        if self.ffmpeg.available():
            probed = self._probe_constituents(path)
            if video_fmt is None and probed.get("vcodec"):
                video_fmt = {"vcodec": probed["vcodec"], "height": probed.get("height") or 0}
            if audio_fmt is None and probed.get("acodec"):
                audio_fmt = {"acodec": probed["acodec"], "tbr": probed.get("abr") or 0}
            meta["probed"] = probed

        plan = plan_for(mode, video_fmt, audio_fmt, self.config.formats)
        folder = Path(staged["folder"])
        stem = staged["stem"]
        final_ext = plan.output_ext
        final_name = f"{stem}.{final_ext}"
        meta["assembled"] = {
            "plan": {
                "mode": plan.mode,
                "action": plan.action,
                "target": plan.target,
                "output_ext": plan.output_ext,
                "ffmpeg_args": plan.ffmpeg_args,
                "video": bool(plan.video),
                "reason": plan.reason,
                "source_codec": plan.source_codec,
            },
            "final_name": final_name,
            "final_dir": str(folder),
            "artifact_id": artifact_id,
            "sha256": validation.sha256,
            "size": validation.bytes,
            "source_path": str(path),
            "video": video_fmt,
            "audio": audio_fmt,
        }
        self.ledger.mark(url, mode, "PROCESSING", meta=meta, checksum=validation.sha256)
        self.ledger.log(f"validated sha256={validation.sha256[:16]}… -> {plan.action}", job_id=job.id)
        self.report(f"assemble [{mode}] {stem}: {plan.action}")
        return self.ledger.get_job(url, mode)  # type: ignore[return-value]

    def process(self, url: str, mode: str) -> Job:
        """Phase 3: FFmpeg remux/transcode, tagging, atomic move, commit."""
        job = self.ledger.get_job(url, mode)
        if job is None:
            raise JobFailed(url, "process called before fetch")
        meta = self._meta_of(job)
        assembled = meta.get("assembled")
        staged = meta.get("staged")
        if not assembled or not staged:
            raise JobFailed(url, "process called before assemble")

        source = Path(assembled["source_path"])
        if not source.exists():
            raise JobFailed(url, f"staged source vanished: {source}")

        plan_data = assembled["plan"]
        plan = MediaPlan(
            mode=plan_data["mode"],
            action=plan_data["action"],
            source_codec=plan_data.get("source_codec") or "",
            target=plan_data["target"],
            output_ext=plan_data["output_ext"],
            ffmpeg_args=list(plan_data.get("ffmpeg_args") or []),
            video=bool(plan_data.get("video")),
            reason=plan_data.get("reason", ""),
        )
        final_name = assembled["final_name"]
        final_dir = Path(assembled["final_dir"])
        artifact_id = int(assembled["artifact_id"])

        needs_ffmpeg = self._needs_post(source, plan, final_name)
        if needs_ffmpeg:
            target = self.staging.intermediate(Path(final_name).stem, f".{plan.output_ext}")
            self.report(f"process [{mode}] ffmpeg: {plan.action}")
            if plan.video:
                self.ffmpeg.remux(source, target, plan.ffmpeg_args)
            else:
                self.ffmpeg.transcode_audio(source, target, plan)
            work = target
        else:
            self.report(f"process [{mode}] stream copy already final: {plan.action}")
            work = source

        info = meta.get("info") or {}
        if mode == "audio":
            self._tag(work, info, job)

        if self.config.write_metadata_json and mode in {"video", "audio"}:
            dump_metadata_json(info, final_dir / f"{Path(final_name).stem}.info.json")

        final_path = self.staging.atomic_move(work, final_dir / final_name)
        final_bytes = file_size(final_path)
        final_sha = sha256_file(final_path) if final_bytes else ""

        self.ledger.update_artifact(
            artifact_id, status="FINAL", path=final_path,
            bytes_written=final_bytes, sha256=final_sha,
        )
        self.ledger.mark(
            url, mode, "COMPLETED",
            output_path=str(final_path), checksum=final_sha, bytes_written=final_bytes, error="",
        )
        self.archive.add(meta.get("staged", {}).get("extractor"), meta.get("staged", {}).get("video_id"))

        # cleanup: everything else that was staged for this job
        leftovers = [Path(p) for p in staged.get("produced", [])]
        for leftover in leftovers:
            if leftover.exists() and leftover != final_path and leftover != source:
                self.staging.cleanup(leftover)
        if source.exists() and source != final_path:
            self.staging.cleanup(source)
        self.ledger.log(f"committed {final_path.name} ({final_bytes} bytes, sha256 {final_sha[:16]}…)",
                        job_id=job.id)
        self.report(f"done [{mode}] {final_path}")
        return self.ledger.get_job(url, mode)  # type: ignore[return-value]

    # ============================================================== metadata
    def run_metadata(self, url: str, hint: dict[str, Any] | None = None) -> JobResult:
        started = time.time()
        hint = hint or {}
        try:
            job = self.ledger.create_job(
                url, "metadata",
                title=hint.get("title"),
                playlist_id=hint.get("playlist_id"),
                playlist_index=hint.get("playlist_index"),
            )
            if self.config.resume and job.status == "COMPLETED" and job.output_path \
                    and Path(job.output_path).exists():
                return self._skip(url, "metadata", "already harvested")

            info = self.client.extract(url, purpose="metadata")
            title = str(info.get("title") or hint.get("title") or "metadata")
            folder = self._folder_for(info, "metadata")
            folder.mkdir(parents=True, exist_ok=True)
            stem = sanitize_filename(title)
            json_path = dump_metadata_json(info, folder / f"{stem}.info.json")

            transcript = self._fetch_transcript(info)
            transcript_path = None
            if transcript:
                transcript_path = folder / f"{stem}.transcript.txt"
                transcript_path.write_text(transcript, encoding="utf-8")

            digest = sha256_file(json_path)
            self.ledger.mark(
                url, "metadata", "COMPLETED",
                output_path=str(json_path), title=title, checksum=digest,
                bytes_written=file_size(json_path),
                meta={"info": _slim_info(info), "transcript": bool(transcript)},
            )
            notes = []
            if transcript_path:
                notes.append(f"transcript: {transcript_path.name}")
            self.report(f"done [metadata] {json_path}")
            return self._result_from_job(url, "metadata", started, notes)
        except Exception as exc:  # noqa: BLE001
            self.gatekeeper.observe(exc, "metadata")
            return self._fail(url, "metadata", exc)

    def _fetch_transcript(self, info: dict[str, Any]) -> str | None:
        pools = [info.get("subtitles") or {}, info.get("automatic_captions") or {}]
        lang_order = ["en", "en-US", "en-GB"]
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            langs = lang_order + [k for k in pool if k not in lang_order]
            for lang in langs:
                entries = pool.get(lang)
                if not entries:
                    continue
                json3 = next((e for e in entries if isinstance(e, dict) and e.get("ext") == "json3"), None)
                if not json3 or not json3.get("url"):
                    continue
                try:
                    raw = self.gatekeeper.fetch(json3["url"], purpose="metadata")
                    text = _transcript_from_json3(raw)
                except Exception as exc:  # noqa: BLE001
                    self.ledger.log(f"transcript fetch failed: {exc}", level="warn")
                    continue
                if text:
                    return text
        return None

    # ============================================================== playlist
    def enumerate_playlist(self, url: str, limit: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        info = self.client.extract(url, purpose="metadata")
        entries = [e for e in (info.get("entries") or []) if e]
        if limit:
            entries = entries[:limit]
        normalized = []
        for index, entry in enumerate(entries, start=1):
            entry_id = entry.get("id")
            webpage = entry.get("webpage_url") or entry.get("url")
            if entry_id and not str(webpage or "").startswith("http"):
                webpage = f"https://www.youtube.com/watch?v={entry_id}"
            normalized.append(
                {
                    "url": webpage,
                    "id": entry_id,
                    "title": entry.get("title") or f"entry {index}",
                    "playlist_id": info.get("id"),
                    "playlist_title": info.get("title"),
                    "playlist_index": entry.get("playlist_index") or index,
                    "duration": entry.get("duration"),
                }
            )
        return info, normalized

    def run_batch(
        self,
        entries: Iterable[dict[str, Any]],
        mode: str,
        playlist_title: str | None = None,
        limit: int | None = None,
    ) -> list[JobResult]:
        """Bounded three-queue pipeline over a playlist."""
        items = [e for e in entries if e.get("url")]
        if limit:
            items = items[:limit]
        for entry in items:
            self.ledger.create_job(
                entry["url"], mode,
                title=entry.get("title"),
                playlist_id=entry.get("playlist_id"),
                playlist_index=entry.get("playlist_index"),
                meta={"playlist_title": entry.get("playlist_title")},
            )
        self.report(
            f"batch [{mode}] {playlist_title or 'playlist'}: {len(items)} entries "
            f"(download={self.config.workers.downloaders}, assemble={self.config.workers.assemblers}, "
            f"transcode={self.config.workers.transcoders})"
        )
        results = asyncio.run(self._run_batch_async(items, mode))
        return results

    async def _run_batch_async(self, items: list[dict[str, Any]], mode: str) -> list[JobResult]:
        budgets = self.config.workers
        pool = WorkerQueues(
            downloaders=budgets.downloaders,
            assemblers=budgets.assemblers,
            transcoders=budgets.transcoders,
            queue_size=budgets.queue_size,
        )
        results: list[JobResult] = []
        async with pool:
            async def pipeline(entry: dict[str, Any], gate: asyncio.Semaphore) -> JobResult:
                url = entry["url"]
                async with gate:
                    started = time.time()
                    try:
                        if mode == "metadata":
                            outcome = await pool.submit("download", self.run_metadata, url, entry)
                            return outcome
                        await pool.submit("download", self.fetch, url, mode, entry)
                        job = self.ledger.get_job(url, mode)
                        if job is None:  # pragma: no cover
                            return JobResult(url, mode, "FAILED", error="job missing")
                        if job.status == "SKIPPED":
                            return self._result_from_job(url, mode, started, ["archive"])
                        if job.status in {"FAILED"}:
                            return self._result_from_job(url, mode, started, [])
                        if job.meta.get("staged"):
                            await pool.submit("assemble", self.assemble, url, mode)
                        job = self.ledger.get_job(url, mode)
                        if job and job.meta.get("assembled") and job.status == "PROCESSING":
                            await pool.submit("transcode", self.process, url, mode)
                        return self._result_from_job(url, mode, started, [])
                    except Exception as exc:  # noqa: BLE001
                        return self._fail(url, mode, exc)

            gate = asyncio.Semaphore(max(1, budgets.downloaders + budgets.assemblers + budgets.transcoders))
            outcomes = await asyncio.gather(
                *(pipeline(entry, gate) for entry in items), return_exceptions=False
            )
            results.extend(outcomes)
            await pool.drain()
        return results

    # ============================================================== internals
    def _progress_hook(self, title: str) -> Callable[[dict], None]:
        if self.config.quiet:
            return lambda d: None
        state: dict[str, float] = {"last": -1.0}

        def hook(d: dict) -> None:
            if d.get("status") != "downloading":
                return
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if not total:
                return
            percent = int(downloaded / total * 100)
            if percent >= state["last"] + 25 or percent >= 100:
                state["last"] = float(percent)
                self.report(f"    {title[:40]:40s} {percent:3d}%")

        return hook

    def _new_files(self, snapshot: set[Path], since: float, stem: str | None = None) -> list[Path]:
        produced = []
        for path in self.staging.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in STAGED_EXT_OK:
                continue
            if _FRAGMENT_RE.search(path.name):
                continue  # yt-dlp intermediate fragment (.f251.webm), never the deliverable
            if path in snapshot:
                try:
                    if path.stat().st_mtime < since - 1:
                        continue
                except OSError:  # pragma: no cover
                    continue
            produced.append(path)
        if stem:
            exact = [p for p in produced if p.stem == stem]
            if exact:
                produced = exact
        return sorted(produced, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _pick_main(produced: list[Path], mode: str) -> Path:
        if mode == "audio":
            audioish = [p for p in produced if p.suffix.lower() in {".m4a", ".opus", ".webm", ".mp3", ".ogg", ".flac", ".wav", ".aac"}]
            if audioish:
                return max(audioish, key=lambda p: p.stat().st_size)
        return max(produced, key=lambda p: p.stat().st_size)

    def _probe_constituents(self, path: Path) -> dict[str, Any]:
        data = self.ffmpeg.probe(path)
        out: dict[str, Any] = {}
        for stream in data.get("streams") or []:
            kind = stream.get("codec_type")
            codec = stream.get("codec_name")
            if kind == "video" and codec not in {"mjpeg", "png", "webp"}:
                out["vcodec"] = PROBE_CODEC_MAP.get(codec, codec)
                out["height"] = stream.get("height") or 0
                out["fps"] = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                if stream.get("color_range") == "tv" and (stream.get("bits_per_raw_sample") or 0) >= 10:
                    out["dynamic_range"] = "HDR"
            elif kind == "audio":
                out["acodec"] = PROBE_CODEC_MAP.get(codec or "", codec)
                out["abr"] = out.get("abr") or 0
        return out

    def _needs_post(self, source: Path, plan: MediaPlan, final_name: str) -> bool:
        if not self.ffmpeg.available():  # pragma: no cover - ffmpeg is required
            return False
        final_ext = Path(final_name).suffix.lower().lstrip(".")
        source_ext = source.suffix.lower().lstrip(".")
        if plan.video:
            # yt-dlp already merged the constituents into the right container
            return source_ext != final_ext
        # audio: skip ffmpeg only when the source is already in the target container
        if plan.mode == "copy" and source_ext == final_ext:
            return False
        if plan.mode == "transcode":
            return True
        return source_ext != final_ext

    def _tag(self, work: Path, info: dict[str, Any], job: Job) -> None:
        cover: bytes | None = None
        lyrics = lyrics_from_info(info)
        if self.config.embed_cover and info.get("thumbnail"):
            try:
                cover = self.gatekeeper.fetch(str(info["thumbnail"]), purpose="media")
            except Exception as exc:  # noqa: BLE001
                self.ledger.log(f"cover fetch failed: {exc}", level="warn", job_id=job.id)
        try:
            tag_audio_file(
                work, info, cover=cover, lyrics=lyrics,
                cover_enabled=self.config.embed_cover,
                lyrics_enabled=self.config.embed_lyrics,
            )
        except MediaError as exc:
            self.ledger.log(f"tagging skipped: {exc}", level="warn", job_id=job.id)

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> "ExecutionEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _slim_info(info: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "id", "title", "uploader", "channel", "upload_date", "duration", "webpage_url",
        "playlist_title", "playlist_index", "thumbnail", "extractor_key", "language",
        "view_count", "like_count", "categories", "tags",
    )
    slim = {k: info.get(k) for k in keep if info.get(k) is not None}
    chapters = info.get("chapters")
    if chapters:
        slim["chapters"] = [
            {"title": c.get("title"), "start_time": c.get("start_time"), "end_time": c.get("end_time")}
            for c in chapters
            if isinstance(c, dict)
        ]
    return slim


def _transcript_from_json3(raw: bytes) -> str | None:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    lines: list[str] = []
    for event in data.get("events") or []:
        segments = event.get("segs")
        if not segments:
            continue
        text = "".join(seg.get("utf8", "") for seg in segments if isinstance(seg, dict))
        if text.strip():
            lines.append(text.strip())
    return "\n".join(lines) if lines else None
