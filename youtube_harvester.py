from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

try:
    import yt_dlp
except ImportError as exc:  # pragma: no cover
    print("This script requires yt-dlp. Install it with: pip install yt-dlp")
    raise

from tqdm import tqdm

OUTPUT_DIR = Path.cwd() / "downloads"
DEFAULT_VIDEO_FORMAT = "bv*+ba/b"
DEFAULT_AUDIO_FORMAT = "bestaudio/best"


def safe_filename(name: str, fallback: str = "video") -> str:
    if not name:
        return fallback
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name or fallback


def format_seconds(seconds: int) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class ProgressTracker:
    def __init__(self) -> None:
        self.current_video = 0
        self.total_videos = 0
        self.current_video_title = ""
        self.overall_pbar = None
        self.video_pbar = None
        self.lock = threading.Lock()

    def setup_overall_progress(self, total: int) -> None:
        self.total_videos = total
        self.overall_pbar = tqdm(
            total=total,
            desc="Overall Progress",
            unit="video",
            position=0,
            leave=True,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} videos [{elapsed}<{remaining}]",
        )

    def setup_video_progress(self, title: str) -> None:
        with self.lock:
            self.current_video_title = title[:50] + "..." if len(title) > 50 else title
            if self.video_pbar:
                self.video_pbar.close()
            self.video_pbar = tqdm(
                total=100,
                desc=f"Downloading: {self.current_video_title}",
                unit="%",
                position=1,
                leave=False,
                bar_format="{l_bar}{bar}| {n_fmt}% [{rate_fmt}]",
            )

    def update_video_progress(self, percent: float) -> None:
        if self.video_pbar is not None and percent is not None:
            with self.lock:
                self.video_pbar.n = max(0, min(float(percent), 100.0))
                self.video_pbar.refresh()

    def complete_video(self) -> None:
        with self.lock:
            if self.video_pbar:
                self.video_pbar.n = 100
                self.video_pbar.refresh()
                self.video_pbar.close()
                self.video_pbar = None
            if self.overall_pbar:
                self.current_video += 1
                self.overall_pbar.update(1)

    def close(self) -> None:
        if self.overall_pbar:
            self.overall_pbar.close()
        if self.video_pbar:
            self.video_pbar.close()


def progress_hook(d: dict, tracker: ProgressTracker) -> None:
    status = d.get("status")
    if status == "downloading":
        percent = 0.0
        downloaded = d.get("downloaded_bytes")
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if downloaded is not None and total:
            percent = (downloaded / total) * 100
        elif "_percent_str" in d:
            try:
                percent = float(str(d["_percent_str"]).replace("%", ""))
            except Exception:
                percent = 0.0
        tracker.update_video_progress(percent)
    elif status == "finished":
        tracker.complete_video()


def parse_timestamp(timestamp_str: str) -> int:
    timestamp_str = timestamp_str.strip()
    if not timestamp_str:
        raise ValueError("Empty timestamp")

    if ":" in timestamp_str:
        parts = timestamp_str.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
        raise ValueError(f"Invalid timestamp: {timestamp_str}")

    return int(timestamp_str)


def get_timestamp_info():
    print("\n🎬 Timestamp Configuration")
    print("=" * 40)

    use_timestamps = input("Do you want to download specific timestamps? (y/n): ").strip().lower()
    if use_timestamps != "y":
        return None

    download_type = input("Download as (1) Video segments or (2) Audio only? Enter 1 or 2: ").strip()
    is_audio_only = download_type == "2"

    timestamps = []
    print("\nEnter start and end times for each segment.")
    print("Press Enter with an empty start time to finish.")
    print("Supported formats: HH:MM:SS, MM:SS, or seconds.")

    while True:
        start_input = input(f"\nStart time for segment #{len(timestamps) + 1}: ").strip()
        if not start_input:
            break

        end_input = input(f"  End time for segment #{len(timestamps) + 1}: ").strip()
        if not end_input:
            print(" End time cannot be empty.")
            continue

        try:
            start_seconds = parse_timestamp(start_input)
            end_seconds = parse_timestamp(end_input)
            if start_seconds >= end_seconds:
                print(" Start time must be before end time.")
                continue
            timestamps.append((start_seconds, end_seconds))
            print(f" Added segment: {start_seconds}s to {end_seconds}s")
        except ValueError:
            print(" Invalid timestamp format. Please use HH:MM:SS, MM:SS, or seconds.")

    if not timestamps:
        print("No timestamps provided. Proceeding with full download.")
        return None

    return {"timestamps": timestamps, "audio_only": is_audio_only}


def build_base_options(tracker: ProgressTracker, outtmpl: str, fmt: str, noplaylist: bool = False) -> dict:
    return {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": noplaylist,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [lambda d: progress_hook(d, tracker)],
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
        "nopart": False,
    }


def build_timed_options(tracker: ProgressTracker, video_info: dict, timestamp_info: dict, segment_index: int) -> dict:
    title = safe_filename(video_info.get("title") or "video")
    playlist_title = safe_filename(video_info.get("playlist_title") or "")
    folder = OUTPUT_DIR / (playlist_title if playlist_title else title)
    folder.mkdir(parents=True, exist_ok=True)

    start, end = timestamp_info["timestamps"][segment_index]
    section = f"*{format_seconds(start)}-{format_seconds(end)}"
    suffix = "mp3" if timestamp_info["audio_only"] else "%(ext)s"

    outtmpl = str(folder / f"{title}_segment_{segment_index + 1}_{start}s-{end}s.{suffix}")
    fmt = DEFAULT_AUDIO_FORMAT if timestamp_info["audio_only"] else DEFAULT_VIDEO_FORMAT

    opts = build_base_options(tracker, outtmpl, fmt, noplaylist=True)

    # Key fix: do not download the full video first.
    # Ask yt-dlp/ffmpeg to fetch only the requested section.
    opts["download_sections"] = [section]
    opts["force_keyframes_at_cuts"] = True

    if timestamp_info["audio_only"]:
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    return opts


def build_full_download_options(tracker: ProgressTracker, info: dict, quality: str = "bv*+ba/b") -> dict:
    title = safe_filename(info.get("title") or "video")
    playlist_title = safe_filename(info.get("playlist_title") or "")
    folder = OUTPUT_DIR / (playlist_title if playlist_title else title)
    folder.mkdir(parents=True, exist_ok=True)
    outtmpl = str(folder / f"{title}.%(ext)s")
    return build_base_options(tracker, outtmpl, quality, noplaylist=False)


def download_with_timestamps(url: str, video_info: dict, timestamp_info: dict, tracker: ProgressTracker) -> None:
    video_title = video_info.get("title", "Unknown Video")
    video_url = video_info.get("webpage_url") or url

    total_segments = len(timestamp_info["timestamps"])
    segment_type = "audio segments" if timestamp_info["audio_only"] else "video segments"
    print(f"\n Downloading {total_segments} {segment_type} from: {video_title}")

    for i, (start, end) in enumerate(timestamp_info["timestamps"]):
        tracker.setup_video_progress(f"{video_title} - Segment {i + 1} ({start}s-{end}s)")
        try:
            opts = build_timed_options(tracker, video_info, timestamp_info, i)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([video_url])
            print(f" Downloaded segment {i + 1}: {start}s to {end}s")
        except Exception as exc:
            print(f" Error downloading segment {i + 1}: {exc}")
            tracker.complete_video()


def download_playlist(url: str, timestamp_info: dict | None = None) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    tracker = ProgressTracker()

    try:
        with yt_dlp.YoutubeDL(build_base_options(tracker, "%(title)s.%(ext)s", DEFAULT_VIDEO_FORMAT)) as ydl:
            print("Fetching playlist information...")
            info = ydl.extract_info(url, download=False)

        # Single video
        if "entries" not in info:
            if timestamp_info:
                print("Processing single video with timestamps...")
                tracker.setup_overall_progress(len(timestamp_info["timestamps"]))
                download_with_timestamps(url, info, timestamp_info, tracker)
            else:
                print(f"Downloading single video: {info.get('title', 'Unknown')}")
                tracker.setup_overall_progress(1)
                tracker.setup_video_progress(info.get("title", "Unknown"))
                with yt_dlp.YoutubeDL(build_full_download_options(tracker, info)) as vdl:
                    vdl.download([url])
                tracker.complete_video()

            tracker.close()
            print(f"\n Download completed. Files saved to: {OUTPUT_DIR}")
            return

        # Playlist
        entries = [e for e in info.get("entries", []) if e is not None]
        total_videos = len(entries)

        print(f"Playlist: {info.get('title', 'Unknown')}")
        print(f"Total videos: {total_videos}")
        if timestamp_info:
            total_segments = total_videos * len(timestamp_info["timestamps"])
            print(f"Total segments to download: {total_segments}")
            tracker.setup_overall_progress(total_segments)
        else:
            tracker.setup_overall_progress(total_videos)

        print(f"Download location: {OUTPUT_DIR}\n")

        for i, entry in enumerate(entries):
            video_title = entry.get("title", f"Video {i + 1}")
            if timestamp_info:
                download_with_timestamps(entry.get("webpage_url") or entry.get("url"), entry, timestamp_info, tracker)
                continue

            video_url = entry.get("webpage_url") or entry.get("url")
            tracker.setup_video_progress(video_title)
            try:
                with yt_dlp.YoutubeDL(build_full_download_options(tracker, entry)) as vdl:
                    vdl.download([video_url])
            except Exception as exc:
                print(f"\n Error downloading {video_title}: {exc}")
                print(" Trying fallback to lower quality...")
                try:
                    fallback_opts = build_full_download_options(tracker, entry, quality="worst")
                    with yt_dlp.YoutubeDL(fallback_opts) as fallback:
                        fallback.download([video_url])
                    print(f" Downloaded {video_title} in lower quality")
                except Exception as exc2:
                    print(f" Failed to download {video_title} even in worst quality: {exc2}")
                    tracker.complete_video()
                    continue

        tracker.close()
        print(f"\n All downloads completed. Files saved to: {OUTPUT_DIR}")

    except Exception as exc:
        tracker.close()
        print(f" Critical error: {exc}")


def main() -> None:
    print("YouTube Playlist Downloader with Timestamp Support")
    print("=" * 60)
    url = input("Enter YouTube playlist/video URL: ").strip()

    if not url:
        print("❗ Error: No URL provided")
        return

    timestamp_info = get_timestamp_info()
    if timestamp_info:
        segment_type = "audio segments" if timestamp_info["audio_only"] else "video segments"
        print("\n Configuration Summary:")
        print(f"   • Download type: {segment_type}")
        print(f"   • Number of timestamps: {len(timestamp_info['timestamps'])}")
        for i, (start, end) in enumerate(timestamp_info["timestamps"]):
            print(f"   • Segment {i + 1}: {start}s to {end}s")

    print()
    download_playlist(url, timestamp_info)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n Download interrupted by user.")
    except Exception as exc:
        print(f"\n Unexpected error: {exc}")
    finally:
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
