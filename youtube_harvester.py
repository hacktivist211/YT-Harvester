import os
import sys
from pathlib import Path
import yt_dlp
from tqdm import tqdm
import threading
import re

class ProgressTracker:
    def __init__(self):
        self.current_video = 0
        self.total_videos = 0
        self.current_video_progress = 0
        self.current_video_title = ""
        self.overall_pbar = None
        self.video_pbar = None
        self.lock = threading.Lock()
        
    def setup_overall_progress(self, total):
        self.total_videos = total
        self.overall_pbar = tqdm(
            total=total,
            desc="Overall Progress",
            unit="video",
            position=0,
            leave=True,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} videos [{elapsed}<{remaining}]"
        )
        
    def setup_video_progress(self, title):
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
                bar_format="{l_bar}{bar}| {n_fmt}% [{rate_fmt}]"
            )
    
    def update_video_progress(self, percent):
        if self.video_pbar and percent is not None:
            with self.lock:
                self.video_pbar.n = min(percent, 100)
                self.video_pbar.refresh()
    
    def complete_video(self):
        with self.lock:
            if self.video_pbar:
                self.video_pbar.n = 100
                self.video_pbar.refresh()
                self.video_pbar.close()
                self.video_pbar = None
            if self.overall_pbar:
                self.current_video += 1
                self.overall_pbar.update(1)
    
    def close(self):
        if self.overall_pbar:
            self.overall_pbar.close()
        if self.video_pbar:
            self.video_pbar.close()

def progress_hook(d, tracker):
    if d['status'] == 'downloading':
        percent = 0
        if d.get('total_bytes') and d.get('downloaded_bytes'):
            percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
        elif d.get('total_bytes_estimate') and d.get('downloaded_bytes'):
            percent = (d['downloaded_bytes'] / d['total_bytes_estimate']) * 100
        elif '_percent_str' in d:
            try:
                percent = float(d['_percent_str'].replace('%', ''))
            except:
                percent = 0
        tracker.update_video_progress(percent)
    elif d['status'] == 'finished':
        tracker.complete_video()

def parse_timestamp(timestamp_str):
    """Parse timestamp string to seconds. Supports formats: HH:MM:SS, MM:SS, or just SS"""
    timestamp_str = timestamp_str.strip()
    
    # Match formats: HH:MM:SS, MM:SS, SS
    if ':' in timestamp_str:
        parts = timestamp_str.split(':')
        if len(parts) == 3:  # HH:MM:SS
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        elif len(parts) == 2:  # MM:SS
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
    else:  # Just seconds
        return int(timestamp_str)

def get_timestamp_info():
    """Get timestamp information from user"""
    print("\n🎬 Timestamp Configuration")
    print("=" * 40)
    
    use_timestamps = input("Do you want to download specific timestamps? (y/n): ").strip().lower()
    if use_timestamps != 'y':
        return None
    
    download_type = input("Download as (1) Video segments or (2) Audio only? Enter 1 or 2: ").strip()
    is_audio_only = download_type == '2'
    
    timestamps = []
    print("\nEnter start and end times for each segment.")
    print("Press Enter with an empty start time to finish.")
    print("Supported formats: HH:MM:SS, MM:SS, or seconds.")
    
    while True:
        # Get start time
        start_input = input(f"\nStart time for segment #{len(timestamps) + 1}: ").strip()
        if not start_input:
            break
            
        # Get end time
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
            print(f" Invalid timestamp format. Please use HH:MM:SS, MM:SS, or seconds.")
            continue
    
    if not timestamps:
        print("No timestamps provided. Proceeding with full download.")
        return None
    
    return {
        'timestamps': timestamps,
        'audio_only': is_audio_only
    }

def get_ydl_opts(tracker, quality='best[height<=1080]', timestamp_info=None, video_title="video", segment_index=None):
    base_path = Path.cwd() / "downloads"
    
    if timestamp_info and timestamp_info['timestamps']:
        # For timestamp downloads
        if timestamp_info['audio_only']:
            format_selector = 'bestaudio/best'
            if segment_index is not None:
                start, end = timestamp_info['timestamps'][segment_index]
                filename = f"%(playlist_title)s/%(title)s_segment_{segment_index+1}_{start}s-{end}s.%(ext)s"
            else:
                filename = f"%(playlist_title)s/%(title)s_audio.%(ext)s"
        else:
            format_selector = quality + '/best[height<=720]/best[height<=480]/best/worst'
            if segment_index is not None:
                start, end = timestamp_info['timestamps'][segment_index]
                filename = f"%(playlist_title)s/%(title)s_segment_{segment_index+1}_{start}s-{end}s.%(ext)s"
            else:
                filename = f"%(playlist_title)s/%(title)s.%(ext)s"
    else:
        # Normal download
        format_selector = quality + '/best[height<=720]/best[height<=480]/best/worst'
        filename = '%(playlist_title)s/%(title)s.%(ext)s'
    
    opts = {
        'format': format_selector,
        'outtmpl': str(base_path / filename),
        'noplaylist': False,
        'quiet': True,
        'ignoreerrors': True,
        'no_warnings': True,
        'progress_hooks': [lambda d: progress_hook(d, tracker)],
        'retries': 3,
        'fragment_retries': 3,
        'sleep_interval': 1,
        'max_sleep_interval': 5,
        'http_chunk_size': 10485760,
    }
    
    # Add timestamp-specific options
    if timestamp_info and segment_index is not None:
        start, end = timestamp_info['timestamps'][segment_index]
        opts['external_downloader'] = 'ffmpeg'
        opts['external_downloader_args'] = {
            'ffmpeg_i': ['-ss', str(start), '-to', str(end)]
        }
        
        if timestamp_info['audio_only']:
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
    
    return opts

def download_with_timestamps(url, video_info, timestamp_info, tracker):
    """Download video segments based on timestamps"""
    video_title = video_info.get('title', 'Unknown Video')
    video_url = video_info.get('webpage_url', url)
    
    total_segments = len(timestamp_info['timestamps'])
    segment_type = "audio segments" if timestamp_info['audio_only'] else "video segments"
    
    print(f"\n Downloading {total_segments} {segment_type} from: {video_title}")
    
    for i, (start, end) in enumerate(timestamp_info['timestamps']):
        segment_title = f"{video_title} - Segment {i+1} ({start}s-{end}s)"
        tracker.setup_video_progress(segment_title)
        
        try:
            opts = get_ydl_opts(tracker, timestamp_info=timestamp_info, segment_index=i)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([video_url])
                print(f" Downloaded segment {i+1}: {start}s to {end}s")
        except Exception as e:
            print(f" Error downloading segment {i+1}: {e}")
            tracker.complete_video()
            continue

def download_playlist(url, timestamp_info=None):
    download_path = Path.cwd() / "downloads"
    download_path.mkdir(exist_ok=True)
    tracker = ProgressTracker()

    try:
        with yt_dlp.YoutubeDL(get_ydl_opts(tracker)) as ydl:
            print("Fetching playlist information...")
            info = ydl.extract_info(url, download=False)

            # Handle single video
            if 'entries' not in info:
                if timestamp_info:
                    print(f"Processing single video with timestamps...")
                    tracker.setup_overall_progress(len(timestamp_info['timestamps']))
                    download_with_timestamps(url, info, timestamp_info, tracker)
                else:
                    print(f"Downloading single video: {info.get('title', 'Unknown')}")
                    tracker.setup_overall_progress(1)
                    tracker.setup_video_progress(info.get('title', 'Unknown'))
                    with yt_dlp.YoutubeDL(get_ydl_opts(tracker)) as vdl:
                        vdl.download([url])
                tracker.close()
                print(f"\n Download completed. Files saved to: {download_path}")
                return

            # Handle playlist
            entries = [e for e in info['entries'] if e is not None]
            total_videos = len(entries)

            print(f"Playlist: {info.get('title', 'Unknown')}")
            print(f"Total videos: {total_videos}")
            
            if timestamp_info:
                total_segments = total_videos * len(timestamp_info['timestamps'])
                print(f"Total segments to download: {total_segments}")
                tracker.setup_overall_progress(total_segments)
            else:
                tracker.setup_overall_progress(total_videos)
            
            print(f"Download location: {download_path}\n")

            for i, entry in enumerate(entries):
                if not entry:
                    continue

                video_title = entry.get('title', f'Video {i+1}')
                
                if timestamp_info:
                    download_with_timestamps(entry.get('webpage_url'), entry, timestamp_info, tracker)
                else:
                    video_url = entry.get('webpage_url')
                    tracker.setup_video_progress(video_title)

                    try:
                        with yt_dlp.YoutubeDL(get_ydl_opts(tracker)) as vdl:
                            vdl.download([video_url])
                    except Exception as e:
                        print(f"\n Error downloading {video_title}: {e}")
                        print(" Trying fallback to lowest quality...")
                        try:
                            fallback_opts = get_ydl_opts(tracker, quality='worst')
                            with yt_dlp.YoutubeDL(fallback_opts) as fallback:
                                fallback.download([video_url])
                                print(f" Downloaded {video_title} in lower quality")
                        except Exception as e2:
                            print(f" Failed to download {video_title} even in worst quality: {e2}")
                        tracker.complete_video()
                        continue

            tracker.close()
            print(f"\n All downloads completed. Files saved to: {download_path}")
            
    except Exception as e:
        tracker.close()
        print(f" Critical error: {str(e)}")

def main():
    print("YouTube Playlist Downloader with Timestamp Support")
    print("=" * 60)
    url = input("Enter YouTube playlist/video URL: ").strip()

    if not url:
        print("❗ Error: No URL provided")
        return

    # Get timestamp configuration
    timestamp_info = get_timestamp_info()
    
    if timestamp_info:
        segment_type = "audio segments" if timestamp_info['audio_only'] else "video segments"
        print(f"\n Configuration Summary:")
        print(f"   • Download type: {segment_type}")
        print(f"   • Number of timestamps: {len(timestamp_info['timestamps'])}")
        for i, (start, end) in enumerate(timestamp_info['timestamps']):
            print(f"   • Segment {i+1}: {start}s to {end}s")

    print()
    download_playlist(url, timestamp_info)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n Download interrupted by user.")
    except Exception as e:
        print(f"\n Unexpected error: {str(e)}")
    finally:
        input("\nPress Enter to exit...")
