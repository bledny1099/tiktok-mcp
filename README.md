# tiktok-mcp

A high-performance Model Context Protocol (MCP) server for extracting TikTok video metadata, transcribing audio locally via Faster-Whisper, and managing media workflows without requiring official TikTok Developer API keys.

## Features

- **Public Scraping**: Extracts metadata, thumbnails, author stats, and media URLs using `yt-dlp` with automatic fallback for geo-restricted or rate-limited videos.
- **Local Speech-to-Text**: High-accuracy speech transcription powered by `faster-whisper` (`large-v3-turbo` / int8 quantization supported).
- **Proxy Support**: Native HTTP & SOCKS5 proxy support for bypassing regional blocks (`TIKTOK_MCP_PROXY`).
- **Flexible Publishing**: Built-in support for uploading and hosting media files locally or to remote storage.
- **Dual Transport**: Supports both `stdio` (for local MCP clients like Claude Desktop / Cursor) and `http` (SSE / Streamable HTTP for remote deployments).

---

## Available MCP Tools

| Tool | Description |
|---|---|
| `get_video_info(url, fast=False)` | Retrieves video title, description, hashtags, duration, author details, and engagement stats. |
| `transcribe_video(url, languages=None, with_timestamps=False, model_size=None, keep_audio=False)` | Downloads the audio track and transcribes speech to text with Whisper. |
| `publish_video(url, slug=None, include_transcript=True, languages=None, max_height=1080)` | Downloads video and thumbnail, transcribes audio, and publishes to configured storage. |
| `batch_video_info(urls, fast=True)` | Processes up to 20 TikTok URLs in parallel. |

---

## Quick Start

### 1. Installation

```bash
git clone https://github.com/bledny1099/tiktok-mcp.git
cd tiktok-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

*System requirement: `ffmpeg` must be installed on your system (`sudo apt install -y ffmpeg`).*

### 2. Pre-downloading Whisper Model (Optional)

```bash
python download_model.py
```

### 3. Running Locally (stdio mode)

For local MCP clients (e.g. Claude Desktop, Cursor, Antigravity IDE):

```json
{
  "mcpServers": {
    "tiktok": {
      "command": "/path/to/tiktok-mcp/.venv/bin/python",
      "args": ["/path/to/tiktok-mcp/server.py"],
      "env": {
        "TIKTOK_MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

### 4. Running as HTTP / SSE Daemon

```bash
export TIKTOK_MCP_TRANSPORT=http
export TIKTOK_MCP_HOST=0.0.0.0
export TIKTOK_MCP_PORT=8770
python server.py
```

---

## Configuration (`.env`)

See `.env.example` for all configurable variables:

- `TIKTOK_MCP_TRANSPORT`: `stdio` or `http` (default: `stdio`).
- `TIKTOK_MCP_PROXY`: Outbound proxy URL (`http://host:port` or `socks5://host:port`).
- `TIKTOK_MCP_WHISPER_MODEL`: Model name (default: `large-v3-turbo`).
- `TIKTOK_MCP_WHISPER_COMPUTE`: Quantization type (`int8`, `float16`, `float32`).
- `TIKTOK_MCP_UPLOAD_MODE`: Media upload target (`off`, `local`, or `http`).

---

## Docker

```bash
docker build -t tiktok-mcp .
docker run -d -p 8770:8770 --name tiktok-mcp tiktok-mcp
```

## License

MIT License.
