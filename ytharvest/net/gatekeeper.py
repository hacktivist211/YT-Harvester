"""Anti-detection egress gatekeeper.

Every network touch in the harvester goes through this object::

    [job] --> Gatekeeper.execute(purpose, fn)
                 │
                 ├─ ClientIdentityRotator   (ios/android/tv/web_creator ...)
                 ├─ POTokenService          (sliding-window proof-of-origin cache)
                 ├─ SessionVault            (encrypted cookies / OAuth, 403+429 rotation)
                 ├─ RateLimiter             (jittered exponential backoff)
                 └─ ProxyMesh               (rotating residential egress + jitter)

``observe()`` is the single funnel for HTTP 403/429 responses: it invalidates
burned credentials, advances the client profile, hops to the next proxy and
asks the rate limiter for a cooled-down retry.
"""

from __future__ import annotations

import itertools
import random
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from ..config import GatekeeperRules
from ..errors import GatekeeperError, RateLimitedError
from .potoken import POTokenService
from .profiles import ClientIdentityRotator, ClientProfile
from .ratelimit import RateLimiter
from .vault import SessionVault

T = TypeVar("T")

HTTP_STATUS_RE = re.compile(r"http error (\d{3})|status(?: code)?[: ]+(\d{3})|\b(403|429|503)\b", re.I)

# yt-dlp player clients that still hand out progressive (video+audio) streams
# when everything else fails - the last rung of the fallback decision tree.
LEGACY_CLIENT_ORDER = ("web", "mweb", "ios", "android", "tv_embedded")


def extract_status(message: str) -> int | None:
    match = HTTP_STATUS_RE.search(message or "")
    if not match:
        return None
    for group in match.groups():
        if group:
            return int(group)
    return None


@dataclass
class GatekeeperStats:
    requests: int = 0
    retries: int = 0
    rotations_403: int = 0
    rotations_429: int = 0
    profile_changes: int = 0
    proxy_changes: int = 0
    cookie_rotations: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rotations_403": self.rotations_403,
            "rotations_429": self.rotations_429,
            "profile_changes": self.profile_changes,
            "proxy_changes": self.proxy_changes,
            "cookie_rotations": self.cookie_rotations,
            "errors": list(self.errors[-10:]),
        }


class ProxyMesh:
    """Rotating proxy pool. ``proxies`` entries look like ``http://user:pass@host:port``."""

    def __init__(self, proxies: Iterable[str] = (), rotate: bool = True, seed: int | None = None) -> None:
        self._proxies = tuple(p.strip() for p in proxies if p and p.strip())
        self._rotate = rotate
        self._rng = random.Random(seed)
        self._cursor = itertools.cycle(range(len(self._proxies))) if self._proxies else None
        self._lock = threading.Lock()
        self.hops = 0

    def __len__(self) -> int:
        return len(self._proxies)

    @property
    def enabled(self) -> bool:
        return bool(self._proxies)

    def current(self) -> str | None:
        if not self._proxies:
            return None
        with self._lock:
            if self._cursor is None:  # pragma: no cover - defensive
                return None
            return self._proxies[next(self._cursor)]

    def next(self) -> str | None:
        if not self._proxies:
            return None
        with self._lock:
            self.hops += 1
            if not self._rotate:
                return self._proxies[0]
            assert self._cursor is not None
            return self._proxies[next(self._cursor)]

    def random_delay(self, jitter: tuple[float, float]) -> float:
        return self._rng.uniform(*jitter)


class EgressGatekeeper:
    """Self-healing egress gatekeeper for the harvester."""

    def __init__(
        self,
        rules: GatekeeperRules | None = None,
        vault: SessionVault | None = None,
        rotator: ClientIdentityRotator | None = None,
        rate_limiter: RateLimiter | None = None,
        po_tokens: POTokenService | None = None,
        proxies: Iterable[str] | None = None,
    ) -> None:
        self.rules = rules or GatekeeperRules()
        self.stats = GatekeeperStats()
        self._lock = threading.RLock()
        self._current_profile: ClientProfile | None = None
        self._current_purpose: str | None = None
        self._active_session: str | None = None
        self._cookie_cache: dict[str, Path] = {}

        proxy_list = tuple(proxies) if proxies is not None else self.rules.normalized_proxies()
        self.proxy_mesh = ProxyMesh(proxy_list, rotate=self.rules.proxy_rotation, seed=self.rules.seed)

        self.rotator = rotator or ClientIdentityRotator(seed=self.rules.seed)
        self.rate_limiter = rate_limiter or RateLimiter(
            base=self.rules.base_delay,
            factor=self.rules.backoff_factor,
            max_delay=self.rules.max_delay,
            jitter=self.rules.jitter_range,
            max_retries=self.rules.max_retries,
            rng=random.Random(self.rules.seed),
        )
        self.po_tokens = po_tokens or POTokenService(
            endpoint=self.rules.po_token_endpoint,
            command=self.rules.po_token_command,
            ttl=self.rules.po_token_ttl,
            static_token=self.rules.po_token_static,
            static_visitor=self.rules.po_token_visitor,
        )
        self.vault = vault
        if self.vault is None and self.rules.vault_path:
            self.vault = SessionVault(self.rules.vault_path, self.rules.vault_passphrase)
        self._session_cycle: list[str] = []

    # ------------------------------------------------------------ profiles
    def profile(self, purpose: str = "media", rotate: bool = False) -> ClientProfile:
        with self._lock:
            if rotate or self._current_profile is None or self._current_purpose != purpose:
                self._current_profile = self.rotator.next(purpose)
                self._current_purpose = purpose
                if rotate:
                    self.stats.profile_changes += 1
            return self._current_profile

    def rotate_profile(self, purpose: str = "media") -> ClientProfile:
        return self.profile(purpose, rotate=True)

    # ------------------------------------------------------------- session
    def import_cookie_file(self, name: str, path: str | Path) -> None:
        """Seed the vault from a raw Netscape cookie file (no browser scraping)."""
        if self.vault is None:  # pragma: no cover - vault always constructed
            raise GatekeeperError("Session vault unavailable")
        entry = self.vault.import_netscape(name, path)
        self._active_session = entry.name

    def load_cookies(self) -> Path | None:
        """Materialize the currently active (decrypted) cookie file for yt-dlp."""
        if self.vault is None:
            return None
        name = self._active_session or self.vault.pick_active("netscape")
        if not name:
            return None
        cached = self._cookie_cache.get(name)
        if cached and cached.exists():
            return cached
        tmp = Path(tempfile.mkdtemp(prefix="ytharvest-")) / f"{name}.cookies.txt"
        self.vault.export_netscape(name, tmp)
        self._cookie_cache[name] = tmp
        self._active_session = name
        return tmp

    def rotate_session(self, reason: str = "") -> str | None:
        """Invalidate burned credentials and switch to the next stored session."""
        if self.vault is None:
            return None
        with self._lock:
            burned = self._active_session or self.vault.pick_active("netscape")
            candidates = [n for n in self.vault.active_names("netscape") if n != burned]
            if burned:
                try:
                    self.vault.mark_rotated(burned)
                except Exception:  # noqa: BLE001
                    pass
            if candidates:
                self._active_session = candidates[0]
                self.stats.cookie_rotations += 1
                cached = self._cookie_cache.pop(burned, None) if burned else None
                if cached and cached.exists():
                    try:
                        cached.unlink()
                    except OSError:  # pragma: no cover
                        pass
                return self._active_session
            # nothing left: burn the current one so the next call starts clean
            if burned:
                try:
                    self.vault.deactivate(burned)
                except Exception:  # noqa: BLE001
                    pass
            self._active_session = None
            return None

    # --------------------------------------------------------------- proxy
    def current_proxy(self) -> str | None:
        return self.proxy_mesh.current() or None

    def rotate_proxy(self) -> str | None:
        proxy = self.proxy_mesh.next()
        if proxy:
            self.stats.proxy_changes += 1
        return proxy

    # ------------------------------------------------------------ po token
    def po_token_args(self, visitor_data: str = "", context: str = "media") -> dict[str, list[str]]:
        try:
            return self.po_tokens.extractor_args(visitor_data, context)
        except Exception as exc:  # noqa: BLE001 - never break a job over a token
            self.stats.errors.append(f"po-token: {exc}")
            return {}

    # ------------------------------------------------------------ yt opts
    def build_options(
        self,
        purpose: str = "media",
        visitor_data: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble the yt-dlp option fragment produced by the gatekeeper."""
        profile = self.profile(purpose)
        opts: dict[str, Any] = {
            "extractor_args": profile.extractor_args(),
            "http_headers": profile.headers(),
            "retries": self.rules.max_retries,
            "fragment_retries": self.rules.max_retries,
            "socket_timeout": 30,
        }
        proxy = self.current_proxy()
        if proxy:
            opts["proxy"] = proxy
        if self.rules.impersonate:
            opts["impersonate"] = self.rules.impersonate  # TLS / JA4 fingerprint
        cookies = self.load_cookies()
        if cookies:
            opts["cookiefile"] = str(cookies)
        po_args = self.po_token_args(visitor_data, purpose)
        if po_args:
            merged = opts.setdefault("extractor_args", {})
            for key, values in po_args.items():
                merged.setdefault(key, []).extend(v for v in values if v not in merged.get(key, []))
        if extra:
            for key, value in extra.items():
                if key == "extractor_args":
                    merged = opts.setdefault("extractor_args", {})
                    for k, v in value.items():
                        merged.setdefault(k, []).extend(v)
                elif key == "http_headers":
                    opts.setdefault("http_headers", {}).update(value)
                else:
                    opts[key] = value
        self.stats.requests += 1
        return opts

    # ---------------------------------------------------------- observation
    def observe(self, error: BaseException | str, purpose: str = "media") -> dict[str, Any]:
        """Feed a failure back into the gatekeeper; returns the reaction taken."""
        message = str(error)
        status = extract_status(message)
        reaction: dict[str, Any] = {"status": status, "actions": []}
        if status is None:
            return reaction

        if status == 403 and self.rules.rotate_on_403:
            with self._lock:
                self.stats.rotations_403 += 1
            session = self.rotate_session("403")
            reaction["actions"].append(f"session:{session or 'none'}")
            profile = self.rotate_profile(purpose)
            reaction["actions"].append(f"profile:{profile.name}")
            self.po_tokens.invalidate()
            reaction["actions"].append("po-token:invalidated")
            if self.proxy_mesh.enabled:
                reaction["actions"].append(f"proxy:{self.rotate_proxy()}")
        elif status == 429 and self.rules.rotate_on_429:
            with self._lock:
                self.stats.rotations_429 += 1
            attempt = self.stats.rotations_429
            delay = self.rate_limiter.next_delay(attempt)
            reaction["retry_after"] = delay
            reaction["actions"].append(f"backoff:{delay:.2f}s")
            if self.proxy_mesh.enabled:
                reaction["actions"].append(f"proxy:{self.rotate_proxy()}")
            reaction["actions"].append(f"profile:{self.rotate_profile(purpose).name}")
        elif status and status >= 500:
            reaction["retry_after"] = self.rate_limiter.next_delay(0)
            reaction["actions"].append(f"backoff:{reaction['retry_after']:.2f}s")
        return reaction

    def note_success(self) -> None:
        with self._lock:
            self.stats.requests += 1

    # -------------------------------------------------------------- execute
    # ------------------------------------------------------------ auxiliary
    def fetch(self, url: str, purpose: str = "media", timeout: float = 30.0) -> bytes:
        """Side-channel GET (thumbnails, transcripts) with the same identity."""
        profile = self.profile(purpose)
        headers = profile.headers()
        proxy = self.current_proxy()
        try:
            from curl_cffi import requests as cffi_requests  # type: ignore
        except Exception:  # noqa: BLE001 - fall back to urllib
            cffi_requests = None  # type: ignore
        if cffi_requests is not None:
            kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": timeout,
                "impersonate": self.rules.impersonate or "chrome",
            }
            if proxy:
                kwargs["proxy"] = proxy
            response = cffi_requests.get(url, **kwargs)
            response.raise_for_status()
            data = response.content
        else:  # pragma: no cover - curl_cffi is a hard dependency in practice
            import urllib.request

            req = urllib.request.Request(url, headers=headers)
            if proxy:
                handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
                opener = urllib.request.build_opener(handler)
                with opener.open(req, timeout=timeout) as resp:  # noqa: S310
                    data = resp.read()
            else:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    data = resp.read()
        self.stats.requests += 1
        return data

    def execute(
        self,
        fn: Callable[..., T],
        *args: object,
        purpose: str = "media",
        key: str = "",
        before_retry: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: object,
    ) -> T:
        """Run ``fn`` with jittered backoff and 403/429 rotation hooks."""
        last: Exception | None = None
        for attempt in range(self.rules.max_retries + 1):
            try:
                result = fn(*args, **kwargs)
                self.note_success()
                return result
            except Exception as exc:  # noqa: BLE001
                last = exc
                status = extract_status(str(exc))
                transient = status in {403, 429, 408, 429, 500, 502, 503, 504} or (
                    status is None and RateLimiter.is_transient(str(exc))
                )
                if not transient or attempt >= self.rules.max_retries:
                    raise
                reaction = self.observe(exc, purpose)
                self.stats.retries += 1
                delay = float(reaction.get("retry_after") or self.rate_limiter.next_delay(attempt))
                if before_retry:
                    before_retry({**reaction, "attempt": attempt, "delay": delay})
                if status == 429:
                    self.rate_limiter.pause(delay)
        raise RateLimitedError(0, str(last))  # pragma: no cover - unreachable
