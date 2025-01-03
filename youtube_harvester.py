import yt_dlp
import os
from pathlib import Path
from typing import Optional, Literal, Dict, Any
from urllib.parse import urlparse, parse_qs
from tqdm import tqdm

class YouTubeDownloader:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self._setup_options()
        self.current_progress_bar = None

    def _setup_options(self):
        """Setup download format options."""
        self.supported_formats = {
            'video': {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
                'merge_output_format': 'avi',
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'avi'
                }],
                'ffmpeg_args': ['-codec:v', 'mpeg4', '-codec:a', 'mp3', '-q:v', '0']
            },
            'audio': {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'flac',
                    'preferredquality': '0'
                }],
                'postprocessor_args': {
                    'FFmpegExtractAudio': ['-acodec', 'flac', '-ar', '96000', '-sample_fmt', 's32', '-ac', '2']
                }
            }
        }

    def _progress_hook(self, d):
        """Custom progress hook for download progress."""
        if d['status'] == 'downloading':
            if not self.current_progress_bar:
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                self.current_progress_bar = tqdm(
                    total=total,
                    unit='B',
                    unit_scale=True,
                    desc="Downloading",
                    ncols=80
                )
            downloaded = d.get('downloaded_bytes', 0)
            self.current_progress_bar.update(downloaded - self.current_progress_bar.n)
        
        elif d['status'] == 'finished':
            if self.current_progress_bar:
                self.current_progress_bar.close()
                self.current_progress_bar = None
                print("\nProcessing downloaded content...")

    def _get_youtube_playlist_id(self, url: str) -> Optional[str]:
        """Extract playlist ID from YouTube URL."""
        parsed_url = urlparse(url)
        if 'youtube.com' in parsed_url.netloc:
            query_params = parse_qs(parsed_url.query)
            return query_params.get('list', [None])[0]
        return None

    def _get_download_options(self, format_type: Literal['video', 'audio']) -> Dict[str, Any]:
        """Get download options based on format type."""
        format_options = self.supported_formats[format_type].copy()
        format_options.update({
            'outtmpl': str(self.output_dir / '%(title)s [%(height)sp].%(ext)s'),
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'prefer_ffmpeg': True,
            'geo_bypass': True,
            'progress_hooks': [self._progress_hook],
        })
        return format_options

    def download_video(self, url: str, format_type: Literal['video', 'audio'] = 'video') -> bool:
        """Download a single video."""
        try:
            with yt_dlp.YoutubeDL(self._get_download_options(format_type)) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    print(f"\nDownload Complete!")
                    print(f"Title: {info.get('title', 'Unknown')}")
                    if format_type == 'video':
                        print(f"Quality: {info.get('height', 'unknown')}p")
                    print(f"Format: {info.get('ext', 'unknown')}")
                return True
        except Exception as e:
            print(f"\nError downloading {url}: {str(e)}")
            return False

    def download_playlist(self, url: str, format_type: Literal['video', 'audio'] = 'video') -> bool:
        """Download a playlist."""
        try:
            with yt_dlp.YoutubeDL({'extract_flat': 'in_playlist', 'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                if 'entries' in info:
                    total = len(info['entries'])
                    print(f"\nFound {total} videos in playlist")
                    
                    for i, entry in enumerate(info['entries'], 1):
                        if entry:
                            print(f"\nProcessing video {i}/{total}")
                            self.download_video(entry['url'], format_type)
                return True
        except Exception as e:
            print(f"\nError processing playlist {url}: {str(e)}")
            return False

def main():
    print("\n=== YouTube Downloader ===")
    
    while True:
        url = input("\nEnter YouTube URL (or 'q' to quit): ").strip()
        if url.lower() == 'q':
            break
            
        if not ('youtube.com' in url or 'youtu.be' in url):
            print("Error: Please enter a valid YouTube URL")
            continue
            
        output_dir = input("Enter output directory path: ").strip()
        if not output_dir:
            print("Error: Output directory path is required")
            continue
            
        format_type = None
        while format_type not in ['1', '2']:
            print("\nSelect download format:")
            print("1. Video (High Quality AVI)")
            print("2. Audio only (High Quality FLAC)")
            format_type = input("Enter your choice (1 or 2): ").strip()
        
        format_type = 'video' if format_type == '1' else 'audio'
        
        try:
            os.makedirs(output_dir, exist_ok=True)
            downloader = YouTubeDownloader(output_dir)
            
            if downloader._get_youtube_playlist_id(url):
                downloader.download_playlist(url, format_type)
            else:
                downloader.download_video(url, format_type)
                
        except KeyboardInterrupt:
            print("\nDownload canceled by user")
            continue
        except Exception as e:
            print(f"\nAn error occurred: {str(e)}")
            continue
        
        if input("\nDownload another? (y/n): ").lower() != 'y':
            break

    print("\nThank you for using YT-Harvester!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProgram terminated by user")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {str(e)}")
