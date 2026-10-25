"""Encrypted session vault: sealing, rotation and Netscape cookie import/export."""

from __future__ import annotations

import os

import pytest

from ytharvest.errors import VaultError
from ytharvest.net.vault import SessionVault

NETSCAPE = """\
# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tg-abc123
.youtube.com\tTRUE\t/\tTRUE\t0\tHSID\tg-def456
"""


@pytest.fixture()
def vault(tmp_path):
    return SessionVault(tmp_path / "session.vault", passphrase="correct horse battery staple")


def test_roundtrip_sealed_payload(vault):
    vault.put("primary", "netscape", NETSCAPE, meta={"source": "manual"})
    entry = vault.get("primary")
    assert entry.kind == "netscape"
    assert "SID\tg-abc123" in entry.payload
    assert entry.meta["source"] == "manual"
    assert entry.active


def test_payload_is_actually_encrypted_on_disk(tmp_path):
    path = tmp_path / "session.vault"
    vault = SessionVault(path, passphrase="key-1")
    vault.put("primary", "netscape", NETSCAPE)
    raw = path.read_text(encoding="utf-8")
    assert "g-abc123" not in raw          # plaintext never hits the disk
    assert "gAAAA" in raw                 # fernet ciphertext present

    reopened = SessionVault(path, passphrase="key-1")
    assert "g-abc123" in reopened.get("primary").payload


def test_wrong_passphrase_is_rejected(tmp_path):
    path = tmp_path / "session.vault"
    SessionVault(path, passphrase="right").put("s", "netscape", NETSCAPE)
    with pytest.raises(VaultError):
        SessionVault(path, passphrase="wrong").list()


def test_missing_passphrase_raises(tmp_path):
    vault = SessionVault(tmp_path / "session.vault")
    with pytest.raises(VaultError):
        vault.put("s", "netscape", NETSCAPE)


def test_import_and_export_netscape_roundtrip(vault, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(NETSCAPE, encoding="utf-8")

    entry = vault.import_netscape("imported", cookie_file)
    assert entry.kind == "netscape"

    exported = vault.export_netscape("imported", tmp_path / "out" / "cookies.txt")
    assert exported.exists()
    assert "SID\tg-abc123" in exported.read_text(encoding="utf-8")
    if os.name != "nt":  # pragma: no cover - windows has no posix modes
        assert oct(exported.stat().st_mode)[-3:] == "600"


def test_import_rejects_non_netscape_files(vault, tmp_path):
    junk = tmp_path / "junk.txt"
    junk.write_text("definitely not a cookie file", encoding="utf-8")
    with pytest.raises(VaultError):
        vault.import_netscape("junk", junk)
    with pytest.raises(VaultError):
        vault.import_netscape("missing", tmp_path / "nope.txt")


def test_rotation_lifecycle(vault, tmp_path):
    for name in ("a", "b", "c"):
        f = tmp_path / f"{name}.txt"
        f.write_text(NETSCAPE, encoding="utf-8")
        vault.import_netscape(name, f)

    assert vault.active_names("netscape") == ["a", "b", "c"]
    first = vault.pick_active("netscape")
    assert first == "a"                                   # least rotated first

    assert vault.mark_rotated(first) == 1
    assert vault.pick_active("netscape") == "b"           # burned session skipped

    vault.deactivate("b")
    assert vault.active_names("netscape") == ["a", "c"]
    assert vault.pick_active("netscape") == "c"

    vault.invalidate("c")
    assert "c" not in vault.names()


def test_expired_entries_are_skipped(vault):
    vault.put(
        "stale", "netscape", NETSCAPE, meta={"expires": 1.0}  # 1970
    )
    assert vault.active_names("netscape") == []
    assert vault.get("stale").is_expired()


def test_unknown_entry_errors(vault):
    with pytest.raises(VaultError):
        vault.get("ghost")
    with pytest.raises(VaultError):
        vault.deactivate("ghost")


def test_env_passphrase_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("YTHARVEST_VAULT_KEY", "env-secret")
    path = tmp_path / "session.vault"
    SessionVault(path).put("s", "oauth", '{"refresh_token": "xyz"}')
    data = SessionVault(path).get("s")
    assert "xyz" in data.payload


def test_oauth_kind_is_not_exportable_as_cookies(vault):
    vault.put("tok", "oauth", '{"refresh_token": "xyz"}')
    with pytest.raises(VaultError):
        vault.export_netscape("tok", "/tmp/whatever.txt")
