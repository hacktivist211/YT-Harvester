# YT-Harvester

A high-performance command-line toolkit built on top of **yt-dlp** for downloading YouTube content with advanced workflow automation. The project provides multiple specialized utilities for downloading videos, extracting high-quality audio, downloading complete playlists, and creating timestamp-based media segments without manually editing videos.

Unlike generic downloaders, **YT-Harvester** focuses on reliability, download monitoring, organized output generation, playlist handling, and precise media extraction through FFmpeg integration.

---

## Features

### High Quality Video Downloads

- Downloads the highest quality video and audio streams available.
- Automatic stream merging.
- Intelligent quality fallback when the preferred format is unavailable.
- Preserves original media quality whenever possible.

---

### High Quality Audio Extraction

- Extracts audio directly from YouTube videos.
- Converts audio into MP3 using FFmpeg.
- Maintains high bitrate output.
- Suitable for music libraries, podcasts, lectures, and archival purposes.

---

### Playlist Downloader

- Downloads entire YouTube playlists automatically.
- Creates organized directory structures based on playlist titles.
- Handles unavailable or private videos gracefully.
- Continues downloading remaining videos even if individual downloads fail.

---

### Timestamp-Based Segment Extraction

One of the primary features of this repository.

Instead of downloading an entire video and manually trimming it afterwards, users can specify one or more timestamp ranges.

Example:

```
00:01:30 → 00:05:20
00:15:10 → 00:18:45
01:03:40 → 01:08:00
```

YT-Harvester automatically extracts each segment into independent media files.

Supports:

- Multiple timestamp ranges
- Video segment extraction
- Audio-only segment extraction
- Single videos
- Entire playlists

---

### Intelligent Progress Monitoring

Unlike the default yt-dlp output, YT-Harvester provides cleaner terminal progress visualization.

Features include:

- Overall playlist progress
- Per-video progress bars
- Segment progress tracking
- Download completion statistics
- Automatic cleanup of completed progress bars

---

### Fault Tolerance

The downloader is designed to recover from common failures.

Includes:

- Automatic retries
- Fragment retries
- Extractor retries
- Download continuation
- Graceful handling of unavailable videos
- Quality fallback mechanism

---

### Organized Output

Downloads are automatically categorized into structured directories.

Examples include:

- Videos
- Audio
- Playlists
- Timestamp Segments

Generated filenames preserve useful metadata such as:

- Video title
- Playlist title
- Playlist index
- Video ID
- Timestamp range

---

# Repository Components

The repository currently contains three independent utilities.

---

## 1. General YouTube Downloader

Provides:

- Single video downloads
- Playlist downloads
- Audio extraction
- Progress bars
- Browser cookie support
- Automatic stream merging

Ideal for everyday downloading.

---

## 2. Playlist Downloader

Optimized specifically for downloading complete playlists.

Capabilities include:

- Individual video progress tracking
- Playlist-wide progress monitoring
- Retry logic
- Automatic directory creation
- Download continuation
- High-quality video downloads

---

## 3. Timestamp Media Downloader

Provides precise media extraction.

Capabilities include:

- Video clipping
- Audio clipping
- Multiple timestamp ranges
- Playlist timestamp extraction
- FFmpeg-based segment creation
- Independent output files for each segment

Useful for:

- Lecture extraction
- Music clips
- Podcasts
- Educational content
- Highlight generation
- Research datasets

---

# Technology Stack

- Python 3
- yt-dlp
- FFmpeg
- tqdm
- pathlib
- threading

---

# Requirements

- Python 3.9+
- FFmpeg installed and accessible from PATH
- Internet connection

---

# Installation

Clone the repository:

```bash
git clone https://github.com/hacktivist211/YT-Harvester.git

cd YT-Harvester
```

Install dependencies:

```bash
pip install yt-dlp tqdm
```

Install FFmpeg.

Verify installation:

```bash
ffmpeg -version
```

---

# Usage

Each utility is designed to run independently.

---

## General Downloader

```bash
python qwertyui.py
```

Example session:

```
Choose an option:

1. Download single video
2. Download playlist
3. Extract audio only
```

---

### Download a Single Video

```text
Option:
1

URL:
https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

---

### Download a Playlist

```text
Option:
2

URL:
https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx
```

---

### Extract MP3 Audio

```text
Option:
3

URL:
https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

---

## Playlist Downloader

Run:

```bash
python Download_High_quality_songs.py
```

Example:

```text
Enter YouTube playlist URL:

https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx
```

The downloader automatically:

- Retrieves playlist metadata
- Displays playlist size
- Downloads every available video
- Tracks overall completion
- Saves files into organized directories

---

## Timestamp Downloader

Run:

```bash
python "Timestamps_High_quality_songs._and_Videos(4K).py"
```

Example:

```
Enter URL:

https://www.youtube.com/watch?v=xxxxxxxx

Download specific timestamps?

y
```

Example timestamps:

```
Start:
00:01:30

End:
00:04:20
```

Second segment:

```
Start:
00:12:10

End:
00:18:40
```

The program creates two independent media files corresponding to the requested intervals.

---

# Timestamp Formats

YT-Harvester accepts multiple timestamp formats.

Examples:

```text
90

01:30

00:01:30

02:15:45
```

All are parsed automatically.

---

# Supported URLs

- Standard YouTube videos
- YouTube playlists
- Public playlists
- Music videos
- Educational videos
- Long-form content

---

# Error Recovery

YT-Harvester automatically handles:

- Interrupted downloads
- Temporary network failures
- Fragment download failures
- Playlist entries that are unavailable
- Unsupported stream formats
- Retry attempts before terminating

---

# Performance

The toolkit minimizes unnecessary overhead by:

- Streaming directly through yt-dlp
- Using FFmpeg only when media processing is required
- Avoiding unnecessary intermediate files
- Tracking downloads efficiently using lightweight progress hooks

---

# Example Commands

Download a single video:

```bash
python qwertyui.py
```

Download an entire playlist:

```bash
python Download_High_quality_songs.py
```

Extract timestamp clips:

```bash
python "Timestamps_High_quality_songs._and_Videos(4K).py"
```

Install dependencies:

```bash
pip install yt-dlp tqdm
```

Verify FFmpeg:

```bash
ffmpeg -version
```

Clone repository:

```bash
git clone https://github.com/hacktivist211/YT-Harvester.git
```

---

# Future Improvements

- Batch URL processing
- Concurrent download scheduling
- Download queue management
- Automatic subtitle downloading
- Metadata export
- SponsorBlock integration
- GUI frontend
- Download history database
- Configuration file support
- Parallel playlist downloads
- Automatic thumbnail embedding

---

# Disclaimer

YT-Harvester is intended for educational, research, and archival purposes. Users are responsible for ensuring that downloaded content complies with YouTube's Terms of Service and all applicable copyright laws within their jurisdiction.
