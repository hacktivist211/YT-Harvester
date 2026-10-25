"""Command line interface: video / audio / metadata / playlist modes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import DEFAULT_IMPERSONATE_TARGETS, FormatRules, GatekeeperRules, HarvestConfig, WorkerRules
from .errors import HarvesterError
from .pipeline.engine import ExecutionEngine, JobResult
from .pipeline.ledger import Ledger

MODES = ("video", "audio", "metadata", "playlist")
AUDIO_CODECS = ("flac", "wav", "opus", "aac", "mp3", "m4a")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytharvest",
        description="YT-Harvest 2.0 - anti-detection, transactional YouTube harvester (yt-dlp + FFmpeg)",
    )
    parser.add_argument("mode", choices=MODES, help="execution mode")
    parser.add_argument("url", nargs="?", help="video / playlist URL")
    parser.add_argument("-o", "--output", default=None, help="output directory (default ./downloads)")
    parser.add_argument("--staging", default=None, help="staging directory for .part chunks")
    parser.add_argument("--ledger", default=None, help="SQLite ledger path")

    # ---- quality matrix -------------------------------------------------
    parser.add_argument("--codec-priority", default=None,
                        help="codec order, highest first (default av01,vp09,avc1)")
    parser.add_argument("--max-height", type=int, default=None, help="resolution cap (e.g. 1080)")
    parser.add_argument("--min-height", type=int, default=None, help="minimum resolution")
    parser.add_argument("--fps", type=int, choices=(24, 30, 50, 60, 120), default=None,
                        help="preferred frame rate")
    parser.add_argument("--hdr", action="store_true", help="prefer HDR10/10-bit over SDR")
    parser.add_argument("--audio-codec", choices=AUDIO_CODECS, default=None,
                        help="audio target codec (audio mode)")
    parser.add_argument("--bitrate-mode", choices=("vbr", "cbr"), default=None,
                        help="audio bitrate mode")
    parser.add_argument("--bitrate", default=None, help="audio bitrate for CBR (e.g. 320k)")
    parser.add_argument("--container", default=None, help="video container override (mp4/mkv)")

    # ---- anti-detection -------------------------------------------------
    parser.add_argument("--proxy", action="append", default=None,
                        help="proxy URL for the rotating mesh (repeatable)")
    parser.add_argument("--no-proxy-rotation", action="store_true",
                        help="pin the first proxy instead of rotating")
    parser.add_argument("--jitter", default=None, metavar="LOW,HIGH",
                        help="jitter window in seconds (default 1,4)")
    parser.add_argument("--base-delay", type=float, default=None, help="backoff base delay")
    parser.add_argument("--retries", type=int, default=None, help="max retries per request")
    po = parser.add_argument_group("proof-of-origin")
    po.add_argument("--po-token-endpoint", default=None, help="remote PO-token solver URL")
    po.add_argument("--po-token-command", default=None,
                    help="local solver template, e.g. 'node solver.js --visitor {visitor_data}'")
    po.add_argument("--po-token", default=None, help="static PO-token (advanced)")
    po.add_argument("--visitor-data", default=None, help="static visitor_data for --po-token")
    session = parser.add_argument_group("session vault")
    session.add_argument("--cookies-file", default=None, help="raw Netscape cookies.txt to import")
    session.add_argument("--vault-key", default=None, help="vault passphrase (or YTHARVEST_VAULT_KEY)")
    session.add_argument("--vault-path", default=None, help="vault file path")
    session.add_argument("--impersonate", default=None,
                         help=f"TLS/JA4 impersonation target {DEFAULT_IMPERSONATE_TARGETS} or 'chrome'")
    session.add_argument("--no-impersonate", action="store_true", help="disable TLS impersonation")

    # ---- workers / pipeline --------------------------------------------
    parser.add_argument("--downloaders", type=int, default=None, help="download queue workers")
    parser.add_argument("--assemblers", type=int, default=None, help="assemble queue workers")
    parser.add_argument("--transcoders", type=int, default=None, help="transcode queue workers")
    parser.add_argument("--queue-size", type=int, default=None, help="bounded queue depth")
    parser.add_argument("--limit", type=int, default=None, help="max playlist entries")
    parser.add_argument("--as", dest="batch_mode", choices=("video", "audio", "metadata"), default=None,
                        help="playlist batch mode (playlist mode only)")
    parser.add_argument("--no-resume", action="store_true", help="ignore ledger/archive resume state")
    parser.add_argument("--no-cover", action="store_true", help="skip cover-art embedding")
    parser.add_argument("--no-lyrics", action="store_true", help="skip lyric embedding")
    parser.add_argument("--no-thumbnails-json", action="store_true", help="skip .info.json sidecars")
    parser.add_argument("--clean-staging", action="store_true", help="purge stale .part chunks first")
    parser.add_argument("--dry-run", action="store_true", help="resolve the fallback chain, download nothing")
    parser.add_argument("--json", action="store_true", help="emit a machine readable summary")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--status", action="store_true", help="print ledger status and exit")
    return parser


def config_from_args(args: argparse.Namespace) -> HarvestConfig:
    jitter = (1.0, 4.0)
    if args.jitter:
        low, _, high = args.jitter.partition(",")
        jitter = (float(low), float(high or low))

    codec_priority = tuple(
        p.strip() for p in (args.codec_priority or "").split(",") if p.strip()
    ) or ("av01", "vp09", "avc1")
    formats = FormatRules(
        codec_priority=codec_priority,
        max_height=args.max_height,
        min_height=args.min_height,
        prefer_fps=args.fps,
        prefer_hdr=args.hdr,
        audio_codec=args.audio_codec or "opus",
        bitrate_mode=args.bitrate_mode or "vbr",
        audio_bitrate=args.bitrate or "192k",
        target_container=args.container,
    )

    gate = GatekeeperRules(
        proxies=tuple(args.proxy or ()),
        proxy_rotation=not args.no_proxy_rotation,
        jitter_range=jitter,
        base_delay=args.base_delay if args.base_delay is not None else 1.0,
        max_retries=args.retries if args.retries is not None else 5,
        po_token_endpoint=args.po_token_endpoint,
        po_token_command=args.po_token_command,
        vault_passphrase=args.vault_key,
        vault_path=Path(args.vault_path) if args.vault_path else None,
        impersonate=None if args.no_impersonate else (args.impersonate or "chrome"),
    )

    workers = WorkerRules(
        downloaders=args.downloaders if args.downloaders is not None else (2 if args.mode == "playlist" else 1),
        assemblers=args.assemblers if args.assemblers is not None else 1,
        transcoders=args.transcoders if args.transcoders is not None else 1,
        queue_size=args.queue_size if args.queue_size is not None else 8,
    )

    cfg = HarvestConfig(
        url=args.url or "",
        mode=args.mode,
        output_dir=Path(args.output) if args.output else Path.cwd() / "downloads",
        staging_dir=Path(args.staging) if args.staging else None,
        ledger_path=Path(args.ledger) if args.ledger else None,
        formats=formats,
        gatekeeper=gate,
        workers=workers,
        playlist_limit=args.limit,
        batch_mode=args.batch_mode or "video",
        resume=not args.no_resume,
        embed_cover=not args.no_cover,
        embed_lyrics=not args.no_lyrics,
        write_metadata_json=not args.no_thumbnails_json,
        dry_run=args.dry_run,
        quiet=args.quiet,
        retries=args.retries if args.retries is not None else 5,
        cookies_file=Path(args.cookies_file) if args.cookies_file else None,
    )
    cfg.gatekeeper.po_token_static = args.po_token
    cfg.gatekeeper.po_token_visitor = args.visitor_data
    return cfg


def print_result(result: JobResult) -> None:
    icon = {"COMPLETED": "OK", "SKIPPED": "==", "FAILED": "XX"}.get(result.status, "??")
    line = f"[{icon}] {result.mode:<9} {result.status:<10} {result.url}"
    if result.output:
        line += f"\n        -> {result.output}"
    if result.selector:
        line += f"\n        selector={result.selector} rung={result.rung}"
    if result.error and result.status == "FAILED":
        line += f"\n        error={result.error[:200]}"
    if result.notes:
        line += f"\n        notes={', '.join(result.notes)}"
    print(line)


def print_summary(results: list[JobResult], engine: ExecutionEngine, as_json: bool = False) -> int:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    failed = [r for r in results if r.status == "FAILED"]
    if as_json:
        payload = {
            "results": [r.to_dict() for r in results],
            "counts": counts,
            "ledger": engine.ledger.stats(),
            "gatekeeper": engine.gatekeeper.stats.to_dict(),
            "po_tokens": engine.gatekeeper.po_tokens.stats(),
            "staging": engine.staging.stats(),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 1 if failed else 0

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for result in results:
        print_result(result)
    print(f"\n  totals: {counts or '{}'}")
    print(f"  ledger: {engine.ledger.stats()}")
    print(f"  gatekeeper: {engine.gatekeeper.stats.to_dict()}")
    print(f"  po-token: {engine.gatekeeper.po_tokens.stats()}")
    print(f"  staging: {engine.staging.stats()}")
    return 1 if failed else 0


def resolve_targets(cfg: HarvestConfig, engine: ExecutionEngine) -> tuple[list[dict[str, Any]], str | None]:
    """Return (entries, playlist_title) for the requested mode."""
    if cfg.mode != "playlist":
        return [{"url": cfg.url}], None
    info, entries = engine.enumerate_playlist(cfg.url, cfg.playlist_limit)
    return entries, str(info.get("title") or "playlist")


def dry_run(cfg: HarvestConfig, engine: ExecutionEngine) -> int:
    url = cfg.url
    if cfg.mode == "playlist":
        info, entries = engine.enumerate_playlist(url, cfg.playlist_limit)
        print(f"playlist: {info.get('title')} ({len(entries)} entries)")
        targets = entries
    else:
        targets = [{"url": url}]
    mode = cfg.batch_mode if cfg.mode == "playlist" else cfg.mode
    if mode == "metadata":
        for target in targets:
            info = engine.client.extract(target["url"], purpose="metadata")
            print(f"metadata: {info.get('title')} -> {len(info.get('chapters') or [])} chapters, "
                  f"{len(info.get('subtitles') or {})} subtitle languages")
        return 0
    for target in targets:
        info = engine.client.extract(target["url"], purpose="metadata")
        manifest = engine.resolver.build_chain(info)
        print(f"\n{info.get('title')}  [{mode}]  target_height={manifest.target_height}")
        for line in manifest.summary():
            print(f"   {line}")
        plan_target = cfg.formats.audio_codec if mode == "audio" else (cfg.formats.target_container or "auto")
        print(f"   plan target: {plan_target}")
    return 0


def show_status(cfg: HarvestConfig) -> int:
    ledger = Ledger(cfg.ledger_path)
    try:
        stats = ledger.stats()
        print("ledger:", stats)
        for status in ("PENDING", "DOWNLOADING", "PROCESSING", "FAILED"):
            jobs = ledger.jobs(status)
            for job in jobs[:20]:
                print(f"  {status:<12} {job.mode:<9} {job.title or job.url}")
                if job.error:
                    print(f"               error: {job.error[:160]}")
        events = ledger.events(10)
        if events:
            print("\nrecent events:")
            for event in events:
                print(f"  [{event['level']}] {event['message'][:140]}")
        return 0
    finally:
        ledger.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = config_from_args(args)

    if args.status:
        return show_status(cfg)
    if not args.url:
        parser.error("a URL is required (or use --status)")
    if args.mode in {"video", "audio"} and "list=" in (args.url or ""):
        # a playlist URL handed to video/audio means "walk the playlist as a batch"
        cfg.mode = "playlist"
        cfg.batch_mode = args.batch_mode or ("audio" if args.audio_codec else "video")
        args.mode = "playlist"

    print(f"YT-Harvest 2.0 :: mode={cfg.mode} output={cfg.output_dir}")
    engine = ExecutionEngine(cfg, reporter=(lambda m: None) if cfg.quiet else print)
    try:
        if cfg.dry_run:
            return dry_run(cfg, engine)

        if cfg.mode == "playlist":
            info, entries = engine.enumerate_playlist(cfg.url, cfg.playlist_limit)
            title = str(info.get("title") or "playlist")
            mode = cfg.batch_mode
            results = engine.run_batch(entries, mode, title, cfg.playlist_limit)
            return print_summary(results, engine, args.json)

        if cfg.mode == "metadata":
            if "list=" in cfg.url:
                info, entries = engine.enumerate_playlist(cfg.url, cfg.playlist_limit)
                title = str(info.get("title") or "playlist")
                results = engine.run_batch(entries, "metadata", title, cfg.playlist_limit)
                return print_summary(results, engine, args.json)
            result = engine.run_metadata(cfg.url)
            return print_summary([result], engine, args.json)

        result = engine.run_job(cfg.url, cfg.mode)
        return print_summary([result], engine, args.json)
    except KeyboardInterrupt:
        print("\ninterrupted - ledger preserved, rerun to resume")
        return 130
    except HarvesterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
