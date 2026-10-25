"""Exception hierarchy shared across the harvester."""

from __future__ import annotations


class HarvesterError(Exception):
    """Base class for every harvester failure."""


class GatekeeperError(HarvesterError):
    """Raised when the egress gatekeeper cannot satisfy a request."""


class RateLimitedError(GatekeeperError):
    """Raised after every retry attempt for a 429/403 style response has been spent."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(message or f"Rate limited with HTTP {status}")


class VaultError(HarvesterError):
    """Raised for session vault problems (missing key, corrupt payload, ...)."""


class FormatSelectionError(HarvesterError):
    """Raised when the format matrix cannot build any usable fallback candidate."""


class ManifestError(HarvesterError):
    """Raised when a stream manifest cannot be parsed/resolved."""


class LedgerError(HarvesterError):
    """Raised for state-ledger failures."""


class StagingError(HarvesterError):
    """Raised when staging validation or the atomic move fails."""


class MediaError(HarvesterError):
    """Raised when FFmpeg / tagging post-processing fails."""


class JobFailed(HarvesterError):
    """Raised when a job exhausts its fallback chain."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Job failed for {url}: {reason}")
