"""YT-Harvest: a transactional, anti-detection YouTube harvester built on yt-dlp + FFmpeg.

Package layout
--------------
``ytharvest.net``      anti-detection egress gatekeeper (identity rotation, PO-token,
                       session vault, jittered rate limiting, proxy mesh, TLS impersonation)
``ytharvest.formats``  dynamic quality/format matrix, fallback decision tree, remux planner
``ytharvest.pipeline`` SQLite WAL state ledger, .part staging, SHA-256 validation, atomic move
``ytharvest.media``    FFmpeg copy/remux/transcode paths, tagging, lyrics, cover art
``ytharvest.workers``  bounded async download / assemble / transcode queues
``ytharvest.cli``      video, audio, metadata and playlist batch modes
"""

from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["__version__"]
