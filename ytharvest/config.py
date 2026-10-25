"""Central configuration objects for the harvester."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Sequence


DEFAULT_CODEC_PRIORITY: tuple[str, ...] = ("av01", "vp09", "avc1")
DEFAULT_IMPERSONATE_TARGETS: tuple[str, ...] = ("chrome", "safari", "firefox")


def _as_tuple(value: Sequence[str] | str | None, default: Sequence[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        parts = tuple(p.strip() for p in value.split(",") if p.strip())
        return parts or tuple(default)
    return tuple(value)


@dataclass
class FormatRules:
    """User facing quality priorities for the dynamic format matrix."""

    codec_priority: tuple[str, ...] = DEFAULT_CODEC_PRIORITY
    max_height: int | None = None
    min_height: int | None = None
    prefer_fps: int | None = None          # 60 or 30; None = ignore frame rate
    prefer_hdr: bool = False               # HDR10/10-bit before SDR when True
    audio_codec: str = "opus"              # flac | wav | opus | aac | mp3
    bitrate_mode: str = "vbr"              # cbr | vbr
    audio_bitrate: str = "192k"            # only used for cbr targets
    target_container: str | None = None    # None -> derive from mode

    def normalized_codecs(self) -> tuple[str, ...]:
        return _as_tuple(self.codec_priority, DEFAULT_CODEC_PRIORITY)


@dataclass
class GatekeeperRules:
    """Anti-detection egress settings."""

    proxies: tuple[str, ...] = ()
    proxy_rotation: bool = True
    jitter_range: tuple[float, float] = (1.0, 4.0)
    base_delay: float = 1.0
    backoff_factor: float = 2.0
    max_delay: float = 60.0
    max_retries: int = 5
    po_token_endpoint: str | None = None      # remote solver service URL
    po_token_command: str | None = None       # local challenge runner command template
    po_token_static: str | None = None        # operator supplied token (advanced)
    po_token_visitor: str | None = None       # visitor_data for the static token
    po_token_ttl: float = 180.0               # sliding window cache lifetime (seconds)
    impersonate: str | None = "chrome"        # JA3/JA4 target, None disables
    rotate_on_403: bool = True
    rotate_on_429: bool = True
    vault_path: Path | None = None
    vault_passphrase: str | None = None
    seed: int | None = None                   # deterministic jitter for tests

    def normalized_proxies(self) -> tuple[str, ...]:
        return _as_tuple(self.proxies, ())


@dataclass
class WorkerRules:
    """Bounded concurrency for the async queues."""

    downloaders: int = 2
    assemblers: int = 1
    transcoders: int = 1
    queue_size: int = 8
    download_concurrency: int = 4            # per job yt-dlp fragment threads

    def validate(self) -> None:
        for name in ("downloaders", "assemblers", "transcoders", "queue_size"):
            if getattr(self, name) < 1:
                raise ValueError(f"worker.{name} must be >= 1")


@dataclass
class HarvestConfig:
    """Everything the CLI and the engine need to run."""

    url: str = ""
    mode: str = "video"                      # video | audio | metadata | playlist
    output_dir: Path = field(default_factory=lambda: Path.cwd() / "downloads")
    staging_dir: Path | None = None           # defaults to output_dir/.staging
    ledger_path: Path | None = None           # defaults to output_dir/harvest.sqlite3
    archive_path: Path | None = None          # yt-dlp download archive (resume)
    formats: FormatRules = field(default_factory=FormatRules)
    gatekeeper: GatekeeperRules = field(default_factory=GatekeeperRules)
    workers: WorkerRules = field(default_factory=WorkerRules)
    playlist_limit: int | None = None
    batch_mode: str = "video"                 # for `playlist` mode: video|audio|metadata
    resume: bool = True
    embed_cover: bool = True
    embed_lyrics: bool = True
    write_metadata_json: bool = True
    dry_run: bool = False
    quiet: bool = False
    retries: int = 5
    cookies_file: Path | None = None          # raw Netscape cookie file import
    write_thumbnail: bool = True

    # ------------------------------------------------------------------ paths
    def __post_init__(self) -> None:
        # every path is normalized to an absolute one so that staged -> final
        # moves can never double-prepend a relative directory
        def _abs(path: Path | str) -> Path:
            return Path(path).expanduser().resolve()

        self.output_dir = _abs(self.output_dir)
        if self.staging_dir is None:
            self.staging_dir = self.output_dir / ".staging"
        else:
            self.staging_dir = _abs(self.staging_dir)
        if self.ledger_path is None:
            self.ledger_path = self.output_dir / "harvest.sqlite3"
        else:
            self.ledger_path = _abs(self.ledger_path)
        if self.archive_path is None:
            self.archive_path = self.output_dir / "archive.txt"
        else:
            self.archive_path = _abs(self.archive_path)
        if self.cookies_file is not None:
            self.cookies_file = _abs(self.cookies_file)
        if self.gatekeeper.vault_path is None:
            self.gatekeeper.vault_path = self.output_dir / "session.vault"
        else:
            self.gatekeeper.vault_path = _abs(self.gatekeeper.vault_path)
        if self.gatekeeper.jitter_range[0] > self.gatekeeper.jitter_range[1]:
            raise ValueError("jitter_range must be (low, high) with low <= high")
        self.workers.validate()

    # ------------------------------------------------------------ inspection
    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("output_dir", "staging_dir", "ledger_path", "archive_path", "cookies_file"):
            value = data.get(key)
            data[key] = str(value) if value is not None else None
        data["gatekeeper"]["vault_path"] = (
            str(self.gatekeeper.vault_path) if self.gatekeeper.vault_path else None
        )
        return data

    @classmethod
    def from_env(cls, **overrides: Any) -> "HarvestConfig":
        """Build a config from ``YTHARVEST_*`` environment variables + overrides."""
        env: dict[str, Any] = {}
        if os.environ.get("YTHARVEST_OUTPUT"):
            env["output_dir"] = Path(os.environ["YTHARVEST_OUTPUT"])
        if os.environ.get("YTHARVEST_PROXIES"):
            env["gatekeeper"] = GatekeeperRules(
                proxies=tuple(
                    p.strip() for p in os.environ["YTHARVEST_PROXIES"].split(",") if p.strip()
                ),
                po_token_endpoint=os.environ.get("YTHARVEST_PO_TOKEN_ENDPOINT"),
            )
        env.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**env)
