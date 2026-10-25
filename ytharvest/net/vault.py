"""Encrypted session vault.

Design rules:

* never scrape the local browser profile files (that is an OS level decryption
  minefield) - the vault only *accepts* material handed to it:
    - raw Netscape cookie files (``cookies.txt``)
    - OAuth2 refresh tokens / access tokens
    - headless browser session harvests (JSON blobs)
* every payload is sealed with a Fernet key derived from a passphrase
  (env ``YTHARVEST_VAULT_KEY`` or ``--vault-key``) via PBKDF2-HMAC-SHA256.
* entries can be invalidated on demand; the gatekeeper invalidates and rotates
  them automatically when it sees HTTP 403 / 429.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken

from ..errors import VaultError

VAULT_VERSION = 1
KDF_ITERATIONS = 200_000
_NETSCAPE_HEADER = "# Netscape HTTP Cookie File"


@dataclass
class VaultEntry:
    name: str
    kind: str                     # netscape | oauth | browser
    payload: str                  # decrypted payload (cookies text / token json)
    created_at: float
    updated_at: float
    rotated: int = 0
    active: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    def cookie_lines(self) -> list[str]:
        if self.kind != "netscape":
            return []
        return [
            line
            for line in self.payload.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
            or line.startswith(_NETSCAPE_HEADER)
        ]

    def is_expired(self, now: float | None = None) -> bool:
        expires = self.meta.get("expires")
        if not expires:
            return False
        return float(expires) <= (now if now is not None else time.time())


class SessionVault:
    """Authenticated encrypted key/value store for session material."""

    def __init__(self, path: str | Path, passphrase: str | None = None) -> None:
        self.path = Path(path)
        self._passphrase = passphrase or os.environ.get("YTHARVEST_VAULT_KEY")
        self._salt: bytes = b""
        self._fernet: Fernet | None = None
        self._entries: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------- crypto
    @staticmethod
    def _derive(passphrase: str, salt: bytes) -> bytes:
        digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, KDF_ITERATIONS, dklen=32)
        return base64.urlsafe_b64encode(digest)

    def _require_passphrase(self) -> str:
        if not self._passphrase:
            raise VaultError(
                "No vault passphrase configured. Set YTHARVEST_VAULT_KEY or pass --vault-key."
            )
        return self._passphrase

    def _load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != VAULT_VERSION:
                raise VaultError(f"Unsupported vault version: {raw.get('version')}")
            self._salt = base64.b64decode(raw["salt"])
            self._entries = raw.get("entries", {})
            if self._passphrase:
                self._fernet = Fernet(self._derive(self._passphrase, self._salt))
                # verify eagerly so a wrong key fails loudly
                for name, entry in self._entries.items():
                    try:
                        self._fernet.decrypt(entry["blob"].encode())
                    except InvalidToken as exc:
                        raise VaultError(f"Cannot decrypt vault entry '{name}' (bad key?)") from exc
        else:
            self._salt = secrets.token_bytes(16)
            self._entries = {}
            if self._passphrase:
                self._fernet = Fernet(self._derive(self._passphrase, self._salt))
        self._loaded = True

    def _seal(self, plaintext: str) -> str:
        if self._fernet is None:
            raise VaultError("Vault is locked; provide a passphrase to seal entries.")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def _open(self, blob: str) -> str:
        if self._fernet is None:
            raise VaultError("Vault is locked; provide a passphrase to open entries.")
        try:
            return self._fernet.decrypt(blob.encode()).decode()
        except InvalidToken as exc:  # pragma: no cover - covered on load
            raise VaultError("Failed to decrypt vault entry") from exc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VAULT_VERSION,
            "salt": base64.b64encode(self._salt).decode(),
            "updated_at": time.time(),
            "entries": self._entries,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - windows
            pass

    # --------------------------------------------------------------- API
    def unlock(self) -> "SessionVault":
        self._load()
        return self

    def put(
        self,
        name: str,
        kind: str,
        payload: str,
        meta: dict[str, Any] | None = None,
        active: bool = True,
    ) -> VaultEntry:
        self._load()
        now = time.time()
        previous = self._entries.get(name)
        entry = {
            "kind": kind,
            "blob": self._seal(payload),
            "created_at": previous["created_at"] if previous else now,
            "updated_at": now,
            "rotated": (previous["rotated"] if previous else 0),
            "active": active,
            "meta": meta or {},
        }
        self._entries[name] = entry
        self._save()
        return VaultEntry(name=name, payload=payload, **{k: v for k, v in entry.items() if k != "blob"})

    def import_netscape(self, name: str, path: str | Path, meta: dict[str, Any] | None = None) -> VaultEntry:
        """Import a raw Netscape ``cookies.txt`` file (no browser scraping)."""
        src = Path(path)
        if not src.exists():
            raise VaultError(f"Cookie file not found: {src}")
        text = src.read_text(encoding="utf-8", errors="replace")
        if not any(
            line.startswith(_NETSCAPE_HEADER) or line.startswith(".youtube.com") for line in text.splitlines()
        ):
            raise VaultError(f"{src} does not look like a Netscape cookie file")
        merged = dict(meta or {})
        merged.setdefault("source_file", str(src))
        return self.put(name, "netscape", text, meta=merged)

    def get(self, name: str) -> VaultEntry:
        self._load()
        entry = self._entries.get(name)
        if entry is None:
            raise VaultError(f"No vault entry named '{name}'")
        payload = self._open(entry["blob"])
        return VaultEntry(name=name, payload=payload, **{k: v for k, v in entry.items() if k != "blob"})

    def list(self) -> list[VaultEntry]:
        self._load()
        return [self.get(name) for name in sorted(self._entries)]

    def names(self, kind: str | None = None) -> list[str]:
        self._load()
        return [
            name
            for name, entry in sorted(self._entries.items())
            if kind is None or entry["kind"] == kind
        ]

    def invalidate(self, name: str) -> None:
        self._load()
        if name in self._entries:
            del self._entries[name]
            self._save()

    def deactivate(self, name: str) -> None:
        """Mark a credential as burned without destroying it (audit trail)."""
        self._load()
        entry = self._entries.get(name)
        if entry is None:
            raise VaultError(f"No vault entry named '{name}'")
        entry["active"] = False
        entry["updated_at"] = time.time()
        self._save()

    def mark_rotated(self, name: str) -> int:
        self._load()
        entry = self._entries.get(name)
        if entry is None:
            raise VaultError(f"No vault entry named '{name}'")
        entry["rotated"] = int(entry.get("rotated", 0)) + 1
        entry["updated_at"] = time.time()
        self._save()
        return entry["rotated"]

    def active_names(self, kind: str | None = None) -> list[str]:
        return [
            e.name
            for e in self.list()
            if e.active and not e.is_expired() and (kind is None or e.kind == kind)
        ]

    def export_netscape(self, name: str, dest: str | Path) -> Path:
        """Materialize a decrypted cookie file for yt-dlp's ``cookiefile``."""
        entry = self.get(name)
        if entry.kind != "netscape":
            raise VaultError(f"Entry '{name}' is not a netscape cookie bundle")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(entry.payload, encoding="utf-8")
        try:
            os.chmod(dest, 0o600)
        except OSError:  # pragma: no cover
            pass
        return dest

    def pick_active(self, kind: str = "netscape") -> str | None:
        names = self.active_names(kind)
        if not names:
            return None
        names.sort(key=lambda n: self._entries[n].get("rotated", 0))
        return names[0]

    def bulk_import(self, items: Iterable[tuple[str, str, str]]) -> None:
        for name, kind, payload in items:
            self.put(name, kind, payload)
