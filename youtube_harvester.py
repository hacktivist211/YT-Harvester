import yt_dlp
import os
from urllib.parse import urlparse, parse_qs

def get_youtube_playlist_id(url):
    parsed_url = urlparse(url)
    if 'youtube.com' in parsed_url.netloc:
        query_params = parse_qs(parsed_url.query)
        return query_params.get('list', [None])[0]
    return None

def download_video(url, output_dir, format_type='video'):
    video_format = {
        'video': {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
            'outtmpl': os.path.join(output_dir, '%(title)s [%(height)sp].%(ext)s'),
            'merge_output_format': 'avi',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'avi'
            }],
            'postprocessor_args': {
                'FFmpegVideoConvertor': ['-codec:v', 'mpeg4', '-codec:a', 'mp3', '-q:v', '0']
            }
        },
        'audio': {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'flac',
                'preferredquality': '0',
            }],
            'postprocessor_args': {
                'FFmpegExtractAudio': ['-acodec', 'flac', '-ar', '96000', '-sample_fmt', 's32', '-ac', '2']
            }
        }
    }

    ydl_opts = {
        **video_format[format_type],
        'ignoreerrors': True,
        'verbose': True,
        'prefer_ffmpeg': True,
        'geo_bypass': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            print(f"\nExtracting {'video' if format_type == 'video' else 'audio'} information...")
            info = ydl.extract_info(url, download=True)
            if info:
                print(f"\nSuccessfully downloaded: {info.get('title', 'Unknown Title')}")
                if format_type == 'video':
                    print(f"Resolution: {info.get('height', 'unknown')}p")
                print(f"Format: {info.get('ext', 'unknown')}")
            return True
        except Exception as e:
            print(f"Error downloading {url}: {str(e)}")
            return False

def download_playlist(url, output_dir, format_type='video'):
    ydl_opts = {
        'extract_flat': 'in_playlist',
        'ignoreerrors': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info:
                total = len(info['entries'])
                print(f"\nFound {total} videos in playlist")
                for i, entry in enumerate(info['entries'], 1):
                    if entry:
                        title = entry.get('title', 'Unknown Title')
                        print(f"\nProcessing {i}/{total}: {title}")
                        download_video(entry['url'], output_dir, format_type)
            return True
        except Exception as e:
            print(f"Error processing playlist {url}: {str(e)}")
            return False

def main():
    print("YouTube High Quality Downloader")
    print("-" * 30)
    
    while True:
        url = input("Enter YouTube URL (or 'q' to quit): ").strip()
        if url.lower() == 'q':
            break
            
        if not ('youtube.com' in url or 'youtu.be' in url):
            print("Error: Please enter a valid YouTube URL")
            continue
            
        output_dir = input("Enter the full output directory path: ").strip()
        if not output_dir:
            print("Error: Output directory path is required")
            continue
            
        while True:
            print("\nChoose download format:")
            print("1. Video (up to 4K quality)")
            print("2. Audio only (FLAC format)")
            choice = input("Enter your choice (1 or 2): ").strip()
            
            if choice in ["1", "2"]:
                format_type = "video" if choice == "1" else "audio"
                break
            print("Invalid choice. Please enter 1 or 2.")

        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            print(f"Error creating directory: {str(e)}")
            continue
        
        try:
            if get_youtube_playlist_id(url):
                download_playlist(url, output_dir, format_type)
            else:
                download_video(url, output_dir, format_type)
        except KeyboardInterrupt:
            print("\nDownload canceled by user")
            continue
        
        print("\nDownload complete!")
        
        if input("\nDownload another? (y/n): ").lower() != 'y':
            break

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProgram terminated by user")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {str(e)}")
