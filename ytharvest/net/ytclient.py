"""yt-dlp transport wrapper driven by the egress gatekeeper.

The wrapper owns *all* yt-dlp interaction so that identity rotation, PO-token
headers, cookie lifecycle, proxy hopping and jittered backoff are applied
uniformly to extraction and download alike.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable

from ..errors import HarvesterError

try:  # pragma: no cover - import guard
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget
except ImportError as exc:  # pragma: no cover
    raise HarvesterError("yt-dlp is required: pip install yt-dlp") from exc

_IMPERSONATE_CACHE: dict[str, "ImpersonateTarget | None"] = {}


def resolve_impersonate(name: str | None) -> "ImpersonateTarget | None":
    """Turn a JA3/JA4 target name into yt-dlp's ImpersonateTarget.

    Returns ``None`` (and the option is dropped) when the runtime cannot
    impersonate that client, so an unsupported name never kills a job.
    """
    if not name:
        return None
    if name in _IMPERSONATE_CACHE:
        return _IMPERSONATE_CACHE[name]
    target: ImpersonateTarget | None = None
    try:
        candidate = ImpersonateTarget.from_str(name)
        with yt_dlp.YoutubeDL({"impersonate": candidate, "quiet": True, "no_warnings": True}):
            target = candidate
    except Exception:  # noqa: BLE001 - unsupported target -> degrade
        target = None
    _IMPERSONATE_CACHE[name] = target
    return target


class YouTubeClient:
    def __init__(self, gatekeeper: Any, quiet: bool = True) -> None:
        self.gatekeeper = gatekeeper
        self.quiet = quiet
        self._lock = threading.Lock()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _base_opts() -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": False,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "skip_download": True,
            "lazy_playlist": False,
        }

    def _merge(self, gate_opts: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = self._base_opts()
        merged.update({k: v for k, v in gate_opts.items() if v is not None})
        # extractor_args / http_headers must be merged, not clobbered
        for key in ("extractor_args", "http_headers"):
            if overrides and key in overrides:
                base = dict(merged.get(key) or {})
                base.update(overrides.pop(key))
                merged[key] = base
        if overrides:
            merged.update(overrides)
        impersonate = merged.pop("impersonate", None)
        resolved = resolve_impersonate(impersonate if isinstance(impersonate, str) else None)
        if resolved is not None:
            merged["impersonate"] = resolved
        return merged

    def _run(self, opts: dict[str, Any], url: str, purpose: str) -> dict[str, Any]:
        def attempt() -> dict[str, Any]:
            with self._lock:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            if info is None:
                raise HarvesterError(f"yt-dlp returned no information for {url}")
            return info

        return self.gatekeeper.execute(attempt, purpose=purpose, key=f"extract:{purpose}")

    # ------------------------------------------------------------------ API
    def extract(
        self,
        url: str,
        purpose: str = "metadata",
        visitor_data: str = "",
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate_opts = self.gatekeeper.build_options(purpose=purpose, visitor_data=visitor_data)
        opts = self._merge(gate_opts, overrides)
        return self._run(opts, url, purpose)

    def extract_flat(self, url: str, purpose: str = "metadata") -> list[dict[str, Any]]:
        """Cheap playlist enumeration (no per-entry extraction)."""
        info = self.extract(
            url,
            purpose=purpose,
            overrides={"extract_flat": "in_playlist", "playlistend": None},
        )
        entries = info.get("entries") or []
        return [dict(e) for e in entries if e]

    def download(
        self,
        url: str,
        *,
        outtmpl: str,
        format_selector: str,
        purpose: str = "media",
        visitor_data: str = "",
        progress_hooks: Iterable[Callable[[dict], None]] = (),
        postprocessor_hooks: Iterable[Callable[[dict], None]] = (),
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate_opts = self.gatekeeper.build_options(purpose=purpose, visitor_data=visitor_data)
        opts = self._merge(gate_opts, overrides)
        opts.update(
            {
                "format": format_selector,
                "outtmpl": {"default": outtmpl},
                "skip_download": False,
                "progress_hooks": list(progress_hooks),
                "postprocessor_hooks": list(postprocessor_hooks),
            }
        )

        def attempt() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True) or {}

        return self.gatekeeper.execute(attempt, purpose=purpose, key=f"download:{purpose}")
