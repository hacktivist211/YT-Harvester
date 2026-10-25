# YT-Harvest 2.0

A **zero-failure, anti-detection, transactional YouTube harvester** built as a Python package around
`yt-dlp` + `FFmpeg`.

The overhaul replaces the original standalone scripts with a layered engine:

1. **Zero-Failure Anti-Detection Engine** — an isolated, self-healing egress gatekeeper
   (client identity rotation, PO-token service, encrypted session vault, jittered backoff,
   rotating proxy mesh, TLS/JA4 fingerprint impersonation).
2. **Dynamic Quality & Format Execution Matrix** — the adaptive manifest is split into
   video-only + audio-only constituents and resolved through a multi-layer rule engine with a
   pre-calculated fallback decision tree and a remux-vs-transcode planner.
3. **Transactional Fault-Tolerant State Engine** — a SQLite WAL ledger, `.part` staging with
   SHA-256 validation, archive-on-resume and an atomic move into the target directory.

---

## Architecture

```
[ Incoming request ]
         │
         ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    Anti-Detection Egress Gatekeeper                   │
│  ytharvest/net/                                                       │
│   • Client Identity Rotator   (ios / android / tv / web_creator)      │
│   • PO-Token Service          (sliding-window cache, remote or CLI)   │
│   • Session Vault             (Fernet-encrypted cookies / OAuth2)     │
│   • Rate Limiter              (t = base·factor^n + random(1.0, 4.0))  │
│   • Rotating Proxy Mesh + TLS/JA4 impersonation (curl_cffi)           │
│   • 403 / 429 hooks           (rotate creds → profile → proxy → wait) │
└──────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  Dynamic Quality & Format Matrix                      │
│  ytharvest/formats/                                                   │
│   1. split manifest → video-only / audio-only / progressive           │
│   2. rank by codec → resolution → fps → HDR                           │
│   3. build the fallback decision tree                                 │
│      Target → next codec @ target res → lower res → combined legacy   │
│   4. plan: stream-copy (bit exact) vs lossless/lossy transcode        │
└──────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    Transactional State Engine                         │
│  ytharvest/pipeline/ + ytharvest/workers/                             │
│   SQLite WAL ledger : PENDING → DOWNLOADING → PROCESSING → COMPLETED │
│   .part staging     : SHA-256 + size + ffprobe container validation   │
│   atomic move       : only after post-processing and tagging succeed  │
│   queues            : bounded download / assemble / transcode pools   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Package layout

```
ytharvest/
├── config.py           HarvestConfig, FormatRules, GatekeeperRules, WorkerRules
├── errors.py           exception hierarchy
├── cli.py              video / audio / metadata / playlist modes
├── net/                ── anti-detection egress ──────────────────────────
│   ├── profiles.py     client identity registry + rotator (iOS/Android/TV/Creator)
│   ├── vault.py        encrypted session vault (cookies, OAuth2, browser harvests)
│   ├── potoken.py      decoupled proof-of-origin solver (HTTP / command / static)
│   ├── ratelimit.py    jittered exponential backoff + retry executor
│   ├── gatekeeper.py   EgressGatekeeper: the single funnel for every request
│   └── ytclient.py     yt-dlp transport wrapper (impersonation, headers, retries)
├── formats/            ── quality execution matrix ────────────────────────
│   ├── matrix.py       codec / resolution / fps / HDR ranking rules
│   ├── resolver.py     manifest split + fallback decision tree
│   └── planner.py      remux-vs-transcode planner (copy, FLAC, WAV, MP3, ...)
├── pipeline/           ── transactional state ─────────────────────────────
│   ├── ledger.py       SQLite WAL ledger (jobs, artifacts, events)
│   ├── staging.py      .part staging, SHA-256 validation, atomic move
│   ├── archive.py      yt-dlp download-archive (archive-on-resume)
│   └── engine.py       ExecutionEngine: fetch → assemble → process
├── media/              ── post-processing ─────────────────────────────────
│   ├── ffmpeg.py       copy / remux / transcode driver + ffprobe
│   └── tagging.py      ID3 / Vorbis / MP4 tags, lyrics, cover art
└── workers/queues.py   bounded async download / assemble / transcode queues
```

---

## Requirements

* Python 3.10+
* FFmpeg + ffprobe on `PATH`
* dependencies: `yt-dlp`, `mutagen`, `cryptography`, `curl_cffi`

```bash
pip install -e .           # or: pip install yt-dlp mutagen cryptography curl_cffi
ffmpeg -version
```

---

## Usage

```bash
python -m ytharvest {video|audio|metadata|playlist} URL [options]
```

### Video mode

```bash
python -m ytharvest video VIDEO_URL -o downloads
python -m ytharvest video PLAYLIST_URL --max-height 1080 --fps 60 --codec-priority av01,vp09,avc1
```

### Audio mode (lossless / lossy targets)

```bash
python -m ytharvest audio PLAYLIST_URL --audio-codec flac          # lossless, level 12
python -m ytharvest audio PLAYLIST_URL --audio-codec wav           # PCM decode
python -m ytharvest audio PLAYLIST_URL --audio-codec opus          # bit-exact stream copy
python -m ytharvest audio PLAYLIST_URL --audio-codec mp3 --bitrate-mode cbr --bitrate 320k
```

### Metadata mode

```bash
python -m ytharvest metadata VIDEO_URL          # .info.json + transcript + chapters
python -m ytharvest metadata PLAYLIST_URL       # batch metadata for every entry
```

### Playlist batch mode

```bash
python -m ytharvest playlist PLAYLIST_URL --as video --limit 5
python -m ytharvest playlist PLAYLIST_URL --as audio --audio-codec flac --downloaders 3
python -m ytharvest playlist PLAYLIST_URL --as metadata
```

### Inspection

```bash
python -m ytharvest video URL --dry-run         # print the resolved fallback tree
python -m ytharvest video URL --status          # ledger state + recent events
python -m ytharvest video URL --json            # machine readable summary
```

---

## 1. Anti-detection engine

| Concern | Flag / component | Behaviour |
|---|---|---|
| Client identity rotation | `ytharvest/net/profiles.py` | `media` → iOS/Android/Android-VR/Safari (high bitrate, minimal JS challenge), `restricted` → TV/TV-embedded (age + region locks), `metadata` → Creator Studio/Web (chapters, transcripts) |
| PO-Token service | `--po-token-endpoint`, `--po-token-command`, `--po-token --visitor-data` | Decoupled solver (HTTP `POST` or local challenge runner), sliding-window cache, invalidated automatically on 403. Missing solver degrades gracefully. |
| Session vault | `--cookies-file`, `--vault-key`, `YTHARVEST_VAULT_KEY` | Fernet-sealed (PBKDF2-HMAC-SHA256, 200k rounds) Netscape cookie / OAuth2 / browser-harvest blobs. **No browser profile scraping.** Burned sessions are rotated on 403/429. |
| Rate limiting | `--jitter LOW,HIGH --base-delay --retries` | `t = min(base·factorⁿ, max) + uniform(1.0, 4.0)` seconds, randomised per attempt so batches never fall into lock-step |
| TLS / JA4 fingerprint | `--impersonate chrome` (default), `--no-impersonate` | yt-dlp `ImpersonateTarget` backed by curl_cffi; unsupported targets are dropped instead of failing |
| Proxy mesh | `--proxy URL` (repeatable), `--no-proxy-rotation` | Round-robin egress with jittered gaps; 403/429 hops to the next proxy |
| Error hooks | automatic | 403 → invalidate session + rotate profile + purge PO-token + hop proxy; 429 → jittered backoff + hop proxy; 5xx → backoff |

Verify what would be used without downloading anything:

```bash
python -m ytharvest video URL --dry-run
```

---

## 2. Dynamic quality & format execution matrix

* **Video criteria** — codec priority (`av01 → vp09 → avc1`), resolution cap/floor,
  preferred frame rate (60 vs 30), HDR10/10-bit vs SDR.
* **Audio criteria** — target delivery codec (`flac`, `wav`, `opus`, `aac`, `mp3`, `m4a`)
  and bitrate mode (`vbr` / `cbr`).

**Fallback decision tree** (each rung is an independent yt-dlp selector, recorded in the ledger):

```
rung 1  target        AV1  2160p -> 399+140
rung 2  alt_codec     VP9  2160p -> 614+140
rung 2  alt_codec     H.264 2160p -> 270+140
rung 3  lower_res     AV1  1080p -> 398+140
        ...
rung 4  legacy        combined progressive / bestaudio/best
```

**Processing plan**

| Request | Source | Action |
|---|---|---|
| Opus | Opus | stream copy, bit exact |
| AAC / M4A | AAC | stream copy + `faststart` |
| FLAC | any | decode → FLAC `-compression_level 12`, source sample rate preserved |
| WAV | any | decode → `pcm_s16le` |
| MP3 VBR | any | `libmp3lame -q:a 0` |
| MP3/AAC/Opus CBR | any | single re-encode at the requested bitrate |
| Video | any | `-c:v copy -c:a copy` remux into the codec-appropriate container (AV1/H.264 → mp4, VP9/HEVC → mkv) |

Tags, lyrics and official artwork are embedded after the encode, **before** the atomic move.

---

## 3. Transactional fault tolerance

```
fetch  ─► SQLite WAL (DOWNLOADING) ─► .part chunk in .staging/
assemble ─► SHA-256 + size + ffprobe validation (PROCESSING)
process ─► FFmpeg → tagging → os.replace() into the target dir (COMPLETED)
```

* every phase writes its progress to `harvest.sqlite3` (WAL, `synchronous=NORMAL`), so a crash
  between phases resumes from the ledger **without re-querying YouTube**
* `.part` chunks are validated (size, SHA-256, container probe) before FFmpeg ever sees them;
  corrupt chunks are dropped and refetched
* the archive file (`archive.txt`) is written only after the atomic move, so re-running a batch
  never re-downloads finished work
* three bounded async queues (`--downloaders`, `--assemblers`, `--transcoders`, `--queue-size`)
  keep sockets and ffmpeg processes under budget

Resume / inspect:

```bash
python -m ytharvest audio PLAYLIST_URL --audio-codec flac   # second run: 0 downloads
python -m ytharvest video PLAYLIST_URL --status
```

---

## Tests

```bash
pip install pytest
python -m pytest tests -q          # 128 tests, offline (no network needed)
```

Coverage highlights:

| File | What it proves |
|---|---|
| `tests/test_formats.py` | codec parsing, matrix ranking (codec/resolution/fps/HDR), the four-rung fallback tree, remux-vs-transcode planning |
| `tests/test_ledger.py` | status lifecycle, error/attempt accounting, crash-resume persistence, concurrent writes |
| `tests/test_staging.py` | `.part` isolation, SHA-256 validation, size/probe failures, atomic move + replace |
| `tests/test_vault.py` | encryption on disk, wrong-key rejection, cookie import/export, rotation lifecycle |
| `tests/test_ratelimit.py` | exponential growth + cap, jitter window, retry semantics |
| `tests/test_gatekeeper.py` | identity rotation, 403 credential rotation, 429 backoff, proxy hops, PO-token cache |
| `tests/test_workers.py` | bounded concurrency, stage isolation, failure propagation |
| `tests/test_engine.py` | offline end-to-end runs with a fake transport + **real FFmpeg**: fallback walk, ledger resume, checksum, atomic move, batching |

### Real end-to-end verification

```bash
python -m ytharvest metadata PLAYLIST_URL -o runs/metadata
python -m ytharvest audio    PLAYLIST_URL -o runs/audio    --audio-codec flac
python -m ytharvest video    PLAYLIST_URL -o runs/video    --max-height 720
python -m ytharvest playlist PLAYLIST_URL -o runs/playlist --as audio --limit 3 --audio-codec mp3
python verify_runs.py                                    # per-mode output verification
```

`verify_runs.py` re-probes every artifact with ffprobe, checks codec/bitrate/height/tags/cover
art, and re-hashes every file against the SHA-256 recorded in the ledger.

Reference run — *Inception (Music from the Motion Picture)*
(`OLAK5uy_lLmpKhtNs73UW3WXKmsMiEK5p-X96j2h4`):

| Mode | Entries | Result |
|---|---|---|
| `metadata` | 12 | 12/12 COMPLETED — `.info.json` sidecars, 1 transcript captured |
| `audio --audio-codec flac` | 12 | 12/12 COMPLETED — FLAC 48 kHz, tags + cover art, ledger SHA-256 verified |
| `video --max-height 720` | 12 | 12/12 COMPLETED — AV1 + AAC in mp4, height ≤ 720 |
| `playlist --as audio --audio-codec mp3` | 3 | 3/3 COMPLETED — MP3 320k CBR, tags + APIC cover |

```
RESULT: ALL CHECKS PASSED
```

---

## Legacy scripts

The original standalone utilities (`qwertyui.py`, `Download_High_quality_songs.py`,
`youtube_harvester.py`) are still in the repository for reference; the `ytharvest` package
supersedes them with every capability they offered (video, audio, playlist, progress reporting,
fault tolerance) plus the ledger, the format matrix and the anti-detection layer.

---

## Disclaimer

YT-Harvest is intended for educational, research and archival purposes. Users are responsible for
ensuring that downloaded content complies with YouTube's Terms of Service and all applicable
copyright laws in their jurisdiction.
