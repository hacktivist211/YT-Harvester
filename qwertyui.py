import yt_dlp
import os
import sys
from tqdm import tqdm

class SilentLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        print(f"ERROR: {msg}")

class TqdmProgressHook:
    def __init__(self):
        self.progress_bars = {}
        self.total_files = 0

    def set_playlist_size(self, total_files):
        self.total_files = total_files

    def __call__(self, d):
        if d['status'] == 'downloading':
            filename = os.path.basename(d['filename'])
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            
            if filename not in self.progress_bars:
                desc = filename
                if "playlist_index" in d and self.total_files > 0:
                    desc = f"({d['playlist_index']}/{self.total_files}) {filename}"
                
                if len(desc) > 50:
                    desc = desc[:47] + "..."
                
                self.progress_bars[filename] = tqdm(
                    total=total_bytes,
                    unit='B',
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=desc,
                    ncols=100,
                    leave=False
                )
            
            pbar = self.progress_bars[filename]
            downloaded = d.get('downloaded_bytes', 0)
            pbar.n = downloaded
            pbar.refresh()

        elif d['status'] == 'finished':
            filename = os.path.basename(d['filename'])
            if filename in self.progress_bars:
                pbar = self.progress_bars[filename]
                pbar.n = pbar.total
                pbar.refresh()
                pbar.close()
                del self.progress_bars[filename]

        elif d['status'] == 'error':
            filename = os.path.basename(d.get('filename', 'Unknown file'))
            if filename in self.progress_bars:
                pbar = self.progress_bars[filename]
                pbar.set_description(f"ERROR: {filename}")
                pbar.close()
                del self.progress_bars[filename]
    
    def close(self):
        for pbar in self.progress_bars.values():
            pbar.close()
        self.progress_bars.clear()

def get_playlist_size(url):
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': 'in_playlist'}) as ydl:
            info = ydl.extract_info(url, download=False)
            return len(info.get('entries', []))
    except Exception:
        return 0

def download_media(url, download_path, ydl_opts, playlist_size=0):
    os.makedirs(download_path, exist_ok=True)
    
    progress_hook = TqdmProgressHook()
    if playlist_size > 0:
        progress_hook.set_playlist_size(playlist_size)
    
    ydl_opts.update({
        'progress_hooks': [progress_hook],
        'logger': SilentLogger(),
        'noprogress': True,
        'quiet': True,
        'cookiesfrombrowser': ('chrome',),
    })

    print(f"Processing URL: {url}")
    print(f"Saving to: {os.path.abspath(download_path)}")
    print("---")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print("\n---")
        print("Operation successful!")
        
    except yt_dlp.utils.DownloadError:
        print("\n---")
        print("Error: Could not process the URL. Please check the URL and your connection.")
        
    except Exception as e:
        print("\n---")
        print(f"An unexpected error occurred: {e}")
    finally:
        progress_hook.close()

def main():
    try:
        import tqdm
    except ImportError:
        print("Error: The 'tqdm' library is not installed.")
        print("Please install it using: pip install tqdm")
        sys.exit(1)

    print("--- YouTube Downloader ---")
    print("Choose an option:")
    print("1. Download single video")
    print("2. Download playlist")
    print("3. Extract audio only")
    
    choice = input("Enter your choice (1-3) [default: 1]: ").strip()
    if not choice:
        choice = '1'
    
    if choice == '1':
        url = input("Enter the YouTube video URL: ").strip()
        if url:
            opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': os.path.join("YouTube_Downloads/Videos", '%(title)s [%(id)s].%(ext)s'),
                'merge_output_format': 'mp4',
            }
            download_media(url, "YouTube_Downloads/Videos", opts)

    elif choice == '2':
        url = input("Enter the YouTube playlist URL: ").strip()
        if url:
            print("Getting playlist information...")
            playlist_size = get_playlist_size(url)
            opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': os.path.join("YouTube_Downloads/Playlists/%(playlist_title)s", '%(playlist_index)s - %(title)s [%(id)s].%(ext)s'),
                'merge_output_format': 'mp4',
                'yes_playlist': True,
            }
            download_media(url, "YouTube_Downloads/Playlists", opts, playlist_size)

    elif choice == '3':
        url = input("Enter the YouTube video URL for audio extraction: ").strip()
        if url:
            opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join("YouTube_Downloads/Audio", '%(title)s [%(id)s].%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
            download_media(url, "YouTube_Downloads/Audio", opts)
    
    else:
        print("Invalid choice. Please run the script again.")

if __name__ == '__main__':
    main()