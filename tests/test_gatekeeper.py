"""Anti-detection gatekeeper: identity rotation, credential rotation, proxies, PO-tokens."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytharvest.config import GatekeeperRules
from ytharvest.net.gatekeeper import EgressGatekeeper, extract_status
from ytharvest.net.potoken import POTokenService
from ytharvest.net.profiles import ClientIdentityRotator, PROFILES

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tg-session-a\n"
)


def make_gatekeeper(tmp_path: Path, **rule_overrides) -> EgressGatekeeper:
    rules = GatekeeperRules(
        seed=99,
        max_retries=3,
        jitter_range=(1.0, 4.0),
        vault_path=tmp_path / "session.vault",
        vault_passphrase="unit-test-key",
        **rule_overrides,
    )
    return EgressGatekeeper(rules)


# ------------------------------------------------------------- status parser
@pytest.mark.parametrize(
    "message,expected",
    [
        ("HTTP Error 403: Forbidden", 403),
        ("HTTP Error 429: Too Many Requests", 429),
        ("error: status code 503", 503),
        ("HTTP Error 404: Not Found", 404),
        ("Private video", None),
        ("", None),
    ],
)
def test_extract_status(message, expected):
    assert extract_status(message) == expected


# ------------------------------------------------------------ client identity
def test_rotator_walks_every_profile_of_a_purpose():
    rotator = ClientIdentityRotator(purposes=("media",))
    seen = [rotator.next("media").name for _ in range(6)]
    assert set(seen) <= {"ios", "android", "android_vr", "web_safari"}
    assert len(set(seen)) >= 3                     # it really rotates


def test_rotator_purpose_specific_clients():
    from ytharvest.net.profiles import PURPOSE_ORDER

    rotator = ClientIdentityRotator()
    for purpose in ("metadata", "restricted", "media"):
        names = {rotator.next(purpose).name for _ in range(4)}
        assert names <= set(PURPOSE_ORDER[purpose])
        assert names


def test_rotator_force_pins_next_identity():
    rotator = ClientIdentityRotator(purposes=("media",))
    assert rotator.force("tv").name == "tv"
    assert rotator.peek("media") in PROFILES.values()


def test_build_options_carries_profile_identity(tmp_path):
    gate = make_gatekeeper(tmp_path)
    opts = gate.build_options(purpose="media")
    assert opts["extractor_args"]["youtube:player_client"] in (
        ["ios"], ["android"], ["android_vr"], ["web_safari"]
    )
    assert "User-Agent" in opts["http_headers"]
    assert opts["impersonate"] == "chrome"          # TLS / JA4 impersonation on
    assert opts["retries"] == 3
    assert opts["socket_timeout"] == 30


def test_metadata_purpose_uses_creator_or_web_client(tmp_path):
    gate = make_gatekeeper(tmp_path)
    for _ in range(4):
        opts = gate.build_options(purpose="metadata")
        assert opts["extractor_args"]["youtube:player_client"][0] in ("web_creator", "web", "web_safari")
        gate.rotate_profile("metadata")


def test_impersonation_can_be_disabled(tmp_path):
    gate = make_gatekeeper(tmp_path, impersonate=None)
    assert "impersonate" not in gate.build_options()


# -------------------------------------------------------------- proxy mesh
def test_proxy_mesh_rotates(tmp_path):
    gate = make_gatekeeper(tmp_path, proxies=("http://p1:8080", "http://p2:8080"))
    assert len(gate.proxy_mesh) == 2
    first = gate.build_options()["proxy"]
    for _ in range(4):
        gate.rotate_proxy()
    seen = {gate.build_options()["proxy"] for _ in range(4)}
    assert first in seen and len(seen) >= 1
    assert gate.stats.proxy_changes >= 4


def test_proxy_mesh_disabled_when_empty(tmp_path):
    gate = make_gatekeeper(tmp_path)
    assert "proxy" not in gate.build_options()
    assert gate.rotate_proxy() is None


def test_static_proxy_does_not_hop(tmp_path):
    gate = make_gatekeeper(
        tmp_path, proxies=("http://only:1",), proxy_rotation=False
    )
    gate.rotate_proxy()
    gate.rotate_proxy()
    assert gate.build_options()["proxy"] == "http://only:1"


# ---------------------------------------------------------- credential hooks
def seed_cookies(gate: EgressGatekeeper, tmp_path: Path, *names: str) -> None:
    for name in names:
        cookie = tmp_path / f"{name}.txt"
        cookie.write_text(NETSCAPE.replace("g-session-a", f"g-session-{name}"), encoding="utf-8")
        gate.import_cookie_file(name, cookie)


def test_cookie_materialization_from_vault(tmp_path):
    gate = make_gatekeeper(tmp_path)
    seed_cookies(gate, tmp_path, "primary")
    exported = gate.load_cookies()
    assert exported is not None and exported.exists()
    assert "SID" in exported.read_text(encoding="utf-8")
    assert gate.build_options()["cookiefile"] == str(exported)


def test_403_rotates_profile_session_and_invalidates_po_token(tmp_path):
    gate = make_gatekeeper(tmp_path, proxies=("http://p1:1", "http://p2:1"))
    seed_cookies(gate, tmp_path, "primary", "backup")

    profile_before = gate.profile("media").name
    session_before = gate._active_session
    po_calls = {"solved": 0}

    def counting_get(*args, **kwargs):
        po_calls["solved"] += 1
        return None

    gate.po_tokens.get = counting_get  # type: ignore[method-assign]

    reaction = gate.observe("HTTP Error 403: Forbidden", purpose="media")

    assert reaction["status"] == 403
    assert gate.stats.rotations_403 == 1
    assert gate._active_session != session_before        # vault credential rotated
    assert gate.stats.cookie_rotations == 1
    assert gate.profile("media").name != profile_before or gate.stats.profile_changes >= 1
    assert any(a.startswith("profile:") for a in reaction["actions"])
    assert any(a.startswith("proxy:") for a in reaction["actions"])
    assert any("po-token" in a for a in reaction["actions"])


def test_429_schedules_jittered_backoff_and_hops_proxy(tmp_path):
    gate = make_gatekeeper(tmp_path, proxies=("http://p1:1", "http://p2:1", "http://p3:1"))
    reaction = gate.observe("HTTP Error 429: Too Many Requests", purpose="media")
    assert reaction["status"] == 429
    delay = reaction["retry_after"]
    assert 1.0 < delay <= 60.0 + 4.0                  # backoff + jitter window
    assert gate.stats.rotations_429 == 1
    assert any(a.startswith("proxy:") for a in reaction["actions"])


def test_server_errors_backoff_without_credential_burn(tmp_path):
    gate = make_gatekeeper(tmp_path)
    reaction = gate.observe("HTTP Error 503: Service Unavailable")
    assert "retry_after" in reaction
    assert gate.stats.rotations_403 == 0
    assert gate.stats.rotations_429 == 0


def test_unknown_errors_are_ignored(tmp_path):
    gate = make_gatekeeper(tmp_path)
    assert gate.observe("no such video") == {"status": None, "actions": []}


def test_rotate_session_without_vault_returns_none(tmp_path):
    gate = EgressGatekeeper(GatekeeperRules(vault_path=None))
    assert gate.rotate_session() is None
    assert gate.load_cookies() is None


# ----------------------------------------------------------------- execution
def test_execute_retries_403_then_succeeds(tmp_path):
    gate = make_gatekeeper(tmp_path, proxies=("http://p1:1",))
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("HTTP Error 403: Forbidden")
        return "ok"

    assert gate.execute(flaky, purpose="media") == "ok"
    assert attempts["n"] == 2
    assert gate.stats.retries == 1


def test_execute_burns_through_retries_then_raises(tmp_path):
    gate = make_gatekeeper(tmp_path)

    def always() -> None:
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    with pytest.raises(RuntimeError):
        gate.execute(always, purpose="media")
    assert gate.stats.retries == 3                    # max_retries for this gate


def test_execute_does_not_retry_permanent_failures(tmp_path):
    gate = make_gatekeeper(tmp_path)
    calls = {"n": 0}

    def fatal() -> None:
        calls["n"] += 1
        raise RuntimeError("Unsupported URL")

    with pytest.raises(RuntimeError):
        gate.execute(fatal, purpose="media")
    assert calls["n"] == 1
    assert gate.stats.retries == 0


# ---------------------------------------------------------------- po tokens
def test_po_token_service_disabled_by_default():
    service = POTokenService()
    assert service.enabled is False
    assert service.get("visitor") is None
    assert service.extractor_args("visitor") == {}


def test_po_token_static_token_is_emitted_as_extractor_args():
    service = POTokenService(static_token="tok123", static_visitor="vis456", ttl=60)
    args = service.extractor_args("vis456")
    assert args["youtube:po_token"] == ["vis456.tok123"]
    assert args["youtube:visitor_data"] == ["vis456"]


def test_po_token_cache_sliding_window():
    clock = {"now": 1000.0}
    service = POTokenService(static_token="t", static_visitor="v", ttl=100, clock=lambda: clock["now"])
    service.get("v")                                 # warm
    hits_before = service.hits
    clock["now"] += 90                               # almost expired
    assert service.get("v") is not None
    assert service.hits == hits_before + 1           # served from cache
    clock["now"] += 90                               # sliding window pushed expiry out
    assert service.get("v") is not None
    assert service.stats()["hits"] >= 2


def test_po_token_remote_failure_is_non_fatal():
    service = POTokenService(endpoint="http://127.0.0.1:1/po", ttl=10, timeout=0.2)
    assert service.enabled
    assert service.get("v") is None                  # solver down -> degrade, not crash
    assert service.stats()["failures"] >= 1


def test_po_token_invalidation_forces_new_solve():
    clock = {"now": 0.0}
    service = POTokenService(static_token="t", static_visitor="v", ttl=100, clock=lambda: clock["now"])
    service.get("v")
    service.invalidate("v")
    assert service.stats()["cached"] == 0
