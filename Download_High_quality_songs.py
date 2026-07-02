import sys
from pathlib import Path
import yt_dlp
from tqdm import tqdm
import threading
import time

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
        if 'total_bytes' in d and d['total_bytes']:
            percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
        elif 'total_bytes_estimate' in d and d['total_bytes_estimate']:
            percent = (d['downloaded_bytes'] / d['total_bytes_estimate']) * 100
        elif '_percent_str' in d:
            try:
                percent = float(d['_percent_str'].replace('%', ''))
            except:
                percent = 0
        else:
            percent = 0
        tracker.update_video_progress(percent)
    elif d['status'] == 'finished':
        tracker.complete_video()

def download_playlist(url):
    download_path = Path.cwd() / "downloads"
    download_path.mkdir(exist_ok=True)
    
    tracker = ProgressTracker()
    
    ydl_opts = {
        'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
        'outtmpl': str(download_path / '%(playlist_title)s/%(title)s.%(ext)s'),
        'writeinfojson': False,
        'writesubtitles': False,
        'writeautomaticsub': False,
        'ignoreerrors': True,
        'no_warnings': True,
        'extractaudio': False,
        'audioformat': 'mp3',
        'embed_subs': False,
        'retries': 3,
        'fragment_retries': 3,
        'extractor_retries': 3,
        'sleep_interval': 1,
        'max_sleep_interval': 5,
        'cookiefile': None,
        'http_chunk_size': 10485760,
        'continuedl': True,
        'progress_hooks': [lambda d: progress_hook(d, tracker)],
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print("Fetching playlist information...")
            info = ydl.extract_info(url, download=False)
            
            if 'entries' not in info:
                print("Error: This doesn't appear to be a playlist URL")
                return
            
            entries = [entry for entry in info['entries'] if entry is not None]
            total_videos = len(entries)
            
            print(f"Playlist: {info.get('title', 'Unknown')}")
            print(f"Total videos: {total_videos}")
            print(f"Download location: {download_path}")
            print()
            
            tracker.setup_overall_progress(total_videos)
            
            for i, entry in enumerate(entries):
                if entry is None:
                    continue
                    
                video_title = entry.get('title', f'Video {i+1}')
                tracker.setup_video_progress(video_title)
                
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as video_ydl:
                        video_ydl.download([entry['webpage_url']])
                except Exception as e:
                    print(f"\nError downloading {video_title}: {str(e)}")
                    tracker.complete_video()
                    continue
            
            tracker.close()
            print(f"\n✓ Download completed! Files saved to: {download_path}")
            
    except Exception as e:
        tracker.close()
        print(f"Error: {str(e)}")
        if "Private video" in str(e) or "Video unavailable" in str(e):
            print("Some videos in the playlist may be private or unavailable.")
        return

def main():
    print("YouTube Playlist Downloader")
    print("=" * 50)
    
    url = input("Enter YouTube playlist URL: ").strip()
    
    if not url:
        print("Error: Please provide a valid URL")
        return
    
    if "playlist" not in url and "list=" not in url:
        print("Warning: This doesn't appear to be a playlist URL")
        confirm = input("Continue anyway? (y/n): ").strip().lower()
        if confirm != 'y':
            return
    
    print()
    confirm = input("Start downloading? (y/n): ").strip().lower()
    if confirm != 'y':
        return
    
    download_playlist(url)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nDownload interrupted by user")
    except Exception as e:
        print(f"\nUnexpected error: {str(e)}")
    finally:
        input("\nPress Enter to exit...")
