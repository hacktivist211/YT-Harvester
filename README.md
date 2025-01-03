# YT-Harvester

A Python-powered tool for downloading high-quality videos and audio from YouTube, supporting playlists, multiple formats, and seamless FFmpeg integration.

## Features

- Download videos in up to 4K resolution (where available)
- Extract high-quality audio in FLAC format (96kHz, 32-bit)
- Support for both playlists and individual videos
- Automatic format detection and conversion
- Simple command-line interface
- Progress tracking for playlist downloads

## Prerequisites

### 1. Python Installation
- Python 3.7 or higher required
- Download from [Python.org](https://www.python.org/downloads/)
- Verify installation with: `python --version`

### 2. Required Python Packages
Install all dependencies using pip:
```bash
pip install yt-dlp
```

### 3. FFmpeg Installation

#### Windows
1. **Download FFmpeg**
   - Visit [FFmpeg GitHub Releases](https://github.com/BtbN/FFmpeg-Builds/releases)
   - Download `ffmpeg-master-latest-win64-gpl.zip`

2. **Installation Steps**
   - Create folder: `C:\Program Files\FFmpeg`
   - Extract the ZIP contents
   - Copy all files from the `bin` folder to `C:\Program Files\FFmpeg`

3. **Add to System PATH**
   - Open System Properties (`Windows + R`, type `sysdm.cpl`)
   - Navigate to Advanced → Environment Variables
   - Under System Variables, edit `Path`
   - Add new entry: `C:\Program Files\FFmpeg`
   - Click OK on all windows

4. **Verify Installation**
   ```bash
   ffmpeg -version
   ```

#### macOS
Using Homebrew:
```bash
brew install ffmpeg
```

#### Linux (Ubuntu/Debian)
```bash
sudo apt update
sudo apt install ffmpeg
```

## Usage Instructions

1. **Start the Program**
   ```bash
   python youtube_harvester.py
   ```

2. **Enter Video URL**
   - For single video: `https://www.youtube.com/watch?v=videoID`
   - For playlist: `https://www.youtube.com/playlist?list=playlistID`

3. **Specify Output Directory**
   - Enter the full path where files should be saved
   - Example: `C:\Downloads\YouTube` or `/home/user/downloads`

4. **Select Format**
   ```
   Choose download format:
   1. Video (up to 4K quality)
   2. Audio only (FLAC format)
   ```

## Example Usage

### Single Video Download
```bash
Enter YouTube URL: https://www.youtube.com/watch?v=videoID
Enter the full output directory path: C:\Downloads\YouTube
Choose download format:
1. Video (up to 4K quality)
2. Audio only (FLAC format)
Enter your choice (1 or 2): 1
```

### Playlist Download
```bash
Enter YouTube URL: https://www.youtube.com/playlist?list=playlistID
Enter the full output directory path: C:\Downloads\YouTube\Playlist
Choose download format:
1. Video (up to 4K quality)
2. Audio only (FLAC format)
Enter your choice (1 or 2): 2
```

## Format Specifications

### Video
- Container: AVI
- Video Codec: MPEG-4
- Quality: Up to 4K (2160p)
- Filename format: `Title [Resolution].avi`

### Audio
- Format: FLAC
- Sample Rate: 96kHz
- Bit Depth: 32-bit
- Channels: Stereo
- Filename format: `Title.flac`

## Troubleshooting

1. **FFmpeg Not Found**
   - Verify FFmpeg is in PATH
   - Restart terminal/command prompt
   - Restart computer if needed

2. **Permission Errors**
   - Ensure write permissions in output directory
   - Run command prompt as administrator (Windows)
   - Use sudo (Linux/macOS)

3. **Download Errors**
   - Check internet connection
   - Verify YouTube URL is accessible
   - Update yt-dlp: `pip install --upgrade yt-dlp`

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

## Contributing

1. Fork the repository
2. Create your feature branch: `git checkout -b feature/NewFeature`
3. Commit your changes: `git commit -m 'Add NewFeature'`
4. Push to the branch: `git push origin feature/NewFeature`
5. Submit a pull request

## Acknowledgments

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) for the core downloading functionality
- [FFmpeg](https://ffmpeg.org/) for media processing capabilities
