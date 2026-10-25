"""Client identity registry + rotation engine.

Directly spoofing a single web client triggers rapid rate limiting, so every
request is assigned a *purpose specific* profile:

* ``media``      -> iOS / Android app clients (high bitrate, minimal JS challenge)
* ``restricted`` -> TV embedded clients (age gates, regional locks, bot detection)
* ``metadata``   -> Web Creator Studio (deep metadata, chapters, transcripts)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ClientProfile:
    """A named client identity that can be handed to yt-dlp."""

    name: str
    purpose: str
    player_clients: tuple[str, ...]
    user_agent: str
    extra_extractor_args: tuple[tuple[str, tuple[str, ...]], ...] = ()
    supports_hdr: bool = True
    note: str = ""

    def extractor_args(self) -> dict[str, list[str]]:
        args: dict[str, list[str]] = {"youtube:player_client": list(self.player_clients)}
        for key, values in self.extra_extractor_args:
            args[key] = list(values)
        return args

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        }


# YouTube's first party clients. ``tv_embedded`` still yields high bitrate
# muxed streams and shrugs off the heaviest bot detection.
_UA_IOS = "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"
_UA_ANDROID = "com.google.android.youtube/19.29.37 (Linux; U; Android 14; en_US) gzip"
_UA_TV = "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 (KHTML, like Gecko) 65.0.3325.181 TV Safari/537.36"
_UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_UA_CREATOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PROFILES: dict[str, ClientProfile] = {
    p.name: p
    for p in (
        ClientProfile(
            name="ios",
            purpose="media",
            player_clients=("ios",),
            user_agent=_UA_IOS,
            note="iOS app client - high bitrate media, minimal JS challenge",
        ),
        ClientProfile(
            name="android",
            purpose="media",
            player_clients=("android",),
            user_agent=_UA_ANDROID,
            note="Android app client - primary high-bitrate stream fetcher",
        ),
        ClientProfile(
            name="android_vr",
            purpose="media",
            player_clients=("android_vr",),
            user_agent=_UA_ANDROID,
            note="Android VR client - extra adaptive stream variants",
        ),
        ClientProfile(
            name="tv",
            purpose="restricted",
            player_clients=("tv",),
            user_agent=_UA_TV,
            note="TV client - age restricted / region locked bypass",
        ),
        ClientProfile(
            name="tv_embedded",
            purpose="restricted",
            player_clients=("tv_embedded",),
            user_agent=_UA_TV,
            note="TV embedded client - heavy bot-detection bypass",
        ),
        ClientProfile(
            name="web_creator",
            purpose="metadata",
            player_clients=("web_creator",),
            user_agent=_UA_CREATOR,
            note="Creator Studio client - chapters, deep metadata, transcripts",
        ),
        ClientProfile(
            name="web",
            purpose="metadata",
            player_clients=("web",),
            user_agent=_UA_WEB,
            note="Plain web client - last resort metadata fetch",
        ),
        ClientProfile(
            name="web_safari",
            purpose="media",
            player_clients=("web_safari",),
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
            ),
            note="Safari web client - alternate TLS/JA4 presentation",
        ),
    )
}

PURPOSE_ORDER: dict[str, tuple[str, ...]] = {
    "media": ("ios", "android", "android_vr", "web_safari"),
    "restricted": ("tv_embedded", "tv", "ios", "android"),
    "metadata": ("web_creator", "web", "web_safari"),
}


class ClientIdentityRotator:
    """Picks and rotates client profiles per purpose.

    Rotation is *round robin* by default (deterministic, spreads load) but can
    be shuffled with ``seed`` so consecutive requests do not follow an obvious
    pattern.
    """

    def __init__(
        self,
        purposes: Iterable[str] | None = None,
        seed: int | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._order: dict[str, list[str]] = {}
        for purpose in purposes or PURPOSE_ORDER:
            names = [n for n in PURPOSE_ORDER[purpose] if n in PROFILES]
            if names:
                self._order[purpose] = names
        self._rng = rng or random.Random(seed)
        self._shuffled = {purpose: list(names) for purpose, names in self._order.items()}
        for names in self._shuffled.values():
            self._rng.shuffle(names)
        self._cursors: dict[str, int] = {p: 0 for p in self._shuffled}
        self.history: list[str] = []

    def available_purposes(self) -> tuple[str, ...]:
        return tuple(self._order)

    def peek(self, purpose: str) -> ClientProfile:
        return PROFILES[self._next_name(purpose, rotate=False)]

    def next(self, purpose: str) -> ClientProfile:
        return PROFILES[self._next_name(purpose, rotate=True)]

    def _next_name(self, purpose: str, *, rotate: bool) -> str:
        names = self._shuffled.get(purpose) or self._shuffled.get("media")
        if not names:  # pragma: no cover - defensive
            raise KeyError(f"Unknown client purpose: {purpose}")
        idx = self._cursors.get(purpose, 0)
        name = names[idx % len(names)]
        if rotate:
            self._cursors[purpose] = idx + 1
            self.history.append(name)
        return name

    def force(self, name: str) -> ClientProfile:
        """Pin the next identity (used after a 403 credential rotation)."""
        if name not in PROFILES:
            raise KeyError(f"Unknown client profile: {name}")
        purpose = PROFILES[name].purpose
        names = self._shuffled.setdefault(purpose, [name])
        if name in names:
            self._cursors[purpose] = names.index(name)
        self.history.append(name)
        return PROFILES[name]

    @staticmethod
    def all_for(purpose: str) -> Sequence[ClientProfile]:
        return [PROFILES[n] for n in PURPOSE_ORDER.get(purpose, ()) if n in PROFILES]
