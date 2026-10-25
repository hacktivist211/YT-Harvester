"""Proof-of-Origin (PO-token) service.

Google's visitor system requires a ``po_token`` (bound to ``visitor_data``)
for some media requests. Rather than embedding a solver inside the hot path,
the harvester talks to a **decoupled** solver:

* ``endpoint`` - an HTTP service (``POST {endpoint}?visitor_data=...`` or a JSON
  body) returning ``{"po_token": ..., "visitor_data": ..., "expires_in": ...}``
* ``command``  - a local headless challenge runner template, e.g.
  ````node po-solver.js --visitor {visitor_data}````` which prints the token

Tokens are cached with a **sliding window**: every successful use refreshes the
expiry so a busy batch never re-runs the challenge mid-flight. When no solver
is configured the service degrades to "disabled" and the caller simply omits
the header (yt-dlp works without PO tokens for most clients).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ..errors import GatekeeperError


@dataclass
class POToken:
    token: str
    visitor_data: str
    expires_at: float
    source: str = "remote"

    def valid(self, now: float) -> bool:
        return bool(self.token) and now < self.expires_at


class POTokenService:
    """Sliding-window PO-token cache backed by a remote/local solver."""

    def __init__(
        self,
        endpoint: str | None = None,
        command: str | None = None,
        ttl: float = 180.0,
        static_token: str | None = None,
        static_visitor: str | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 20.0,
    ) -> None:
        self.endpoint = endpoint
        self.command = command
        self.ttl = ttl
        self.clock = clock
        self.timeout = timeout
        self._cache: dict[str, POToken] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.failures = 0
        if static_token:
            self._cache["__static__"] = POToken(
                token=static_token,
                visitor_data=static_visitor or "",
                expires_at=self.clock() + self.ttl,
                source="static",
            )

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint or self.command or "__static__" in self._cache)

    # ---------------------------------------------------------------- API
    def get(self, visitor_data: str = "", context: str = "media") -> POToken | None:
        """Return a (possibly cached) valid token or ``None`` when disabled."""
        if not self.enabled:
            return None
        key = f"{context}:{visitor_data}"
        now = self.clock()
        with self._lock:
            cached = self._cache.get(key) or self._cache.get("__static__")
            if cached and cached.valid(now):
                # sliding window: every use pushes the expiry forward
                cached.expires_at = min(now + self.ttl, cached.expires_at + self.ttl)
                self.hits += 1
                return cached
        self.misses += 1
        token = self._solve(visitor_data, context)
        if token is None:
            self.failures += 1
            return None
        with self._lock:
            self._cache[key] = token
        return token

    def invalidate(self, visitor_data: str = "", context: str = "media") -> None:
        with self._lock:
            self._cache.pop(f"{context}:{visitor_data}", None)
            self._cache.pop("__static__", None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "cached": len(self._cache),
                "hits": self.hits,
                "misses": self.misses,
                "failures": self.failures,
            }

    def extractor_args(self, visitor_data: str = "", context: str = "media") -> dict[str, list[str]]:
        """yt-dlp ``extractor-args`` fragment for the current token."""
        token = self.get(visitor_data, context)
        if token is None:
            return {}
        args = {"youtube:po_token": [f"{token.visitor_data}.{token.token}"]}
        if token.visitor_data:
            args["youtube:visitor_data"] = [token.visitor_data]
        return args

    # ------------------------------------------------------------- solvers
    def _solve(self, visitor_data: str, context: str) -> POToken | None:
        if self.endpoint:
            try:
                return self._solve_remote(visitor_data, context)
            except Exception:  # noqa: BLE001 - solver outages must not kill the job
                self.failures += 1
        if self.command:
            try:
                return self._solve_local(visitor_data, context)
            except Exception:  # noqa: BLE001
                self.failures += 1
        return None

    def _solve_remote(self, visitor_data: str, context: str) -> POToken:
        payload = json.dumps(
            {"visitor_data": visitor_data, "context": context, "client": "yt-harvest"}
        ).encode()
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "yt-harvest/2.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                body = json.loads(resp.read().decode() or "{}")
        except urllib.error.URLError as exc:
            raise GatekeeperError(f"PO-token solver unreachable: {exc}") from exc
        token = str(body.get("po_token") or body.get("token") or "")
        if not token:
            raise GatekeeperError("PO-token solver returned no token")
        expires_in = float(body.get("expires_in", self.ttl))
        return POToken(
            token=token,
            visitor_data=str(body.get("visitor_data") or visitor_data),
            expires_at=self.clock() + min(expires_in, self.ttl),
            source="remote",
        )

    def _solve_local(self, visitor_data: str, context: str) -> POToken:
        template = self.command or ""
        cmd = template.format(visitor_data=visitor_data or "unknown", context=context)
        proc = subprocess.run(  # noqa: S603
            shlex.split(cmd, posix=True),
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise GatekeeperError(f"PO-token runner exited {proc.returncode}: {proc.stderr[:200]}")
        out = proc.stdout.strip()
        data: dict[str, Any]
        if out.startswith("{"):
            data = json.loads(out)
        else:
            data = {"po_token": out.splitlines()[0] if out else ""}
        token = str(data.get("po_token") or "")
        if not token:
            raise GatekeeperError("PO-token runner produced no token")
        return POToken(
            token=token,
            visitor_data=str(data.get("visitor_data") or visitor_data),
            expires_at=self.clock() + min(float(data.get("expires_in", self.ttl)), self.ttl),
            source="local",
        )
