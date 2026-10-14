<p align="center">
  <img src="assets/logo.png" alt="TikTok Logo" width="240" />
</p>

<h1 align="center">TikTok MCP Server</h1>

<p align="center">
  Model Context Protocol server for extracting TikTok video and photo-carousel metadata, transcribing speech with Faster-Whisper, reading on-screen screenshots and slides with Tesseract OCR, and automating media workflows.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Protocol-MCP-8A2BE2?style=flat" alt="MCP" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat" alt="License" />
</p>

<p align="center">
  <a href="#overview">Overview</a> •
  <a href="#available-tools">Tools</a> •
  <a href="#installation-and-setup">Setup</a> •
  <a href="#configuration-reference">Config</a> •
  <a href="#security">Security</a> •
  <a href="#production-deployment">Deployment</a> •
  <a href="#troubleshooting">Troubleshooting</a>
</p>

---

## Overview

This MCP server connects your AI assistant (Claude Desktop, Antigravity IDE, Cursor, etc.) directly to [TikTok](https://tiktok.com). It pulls video descriptions, author statistics, tags, media streams, speech-to-text transcriptions, and on-screen OCR text from screenshots and photo slideshows directly into your workspace.

### Key capabilities

- **Multi-tier extraction with fallback**: Extracts video and photo-mode metadata, view counts, and direct streams via `yt-dlp`, with automatic fallback to `tikwm` and TikTok's anonymous `embed/v2` endpoint when pages hit anti-bot challenges.
- **Local Speech-to-Text**: Fast speech transcription powered by `faster-whisper` (`large-v3-turbo` with `int8` quantization support). Audio is processed locally on your machine.
- **Visual OCR for Screenshots & Photo Carousels**: Automatically extracts keyframes from videos (`ffmpeg` scene-change + interval selection), deduplicates identical frames via 256-bit center-crop `dHash`, preprocesses dark-mode and light-mode screenshots with `Pillow`, and runs `tesseract` (`rus+eng`) to read chat logs, code snippets, articles, and TikTok Photo Mode (`/photo/`) carousels.
- **Outbound proxy support**: Native HTTP and SOCKS5 proxy routing (`TIKTOK_MCP_PROXY`) to handle cross-border blocks and georestricted videos.
- **Custom domain publishing**: Download media and host video/cover assets directly on your own domain or CDN.
- **No TikTok Developer API**: No OAuth, no app registration, no API keys required.

---

## Available Tools

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `get_video_info` | `url` (string), `fast` (bool, default: False) | Returns post title, description, hashtags, links, author, stats, cover image, and `carousel_items` for photo slideshows. `fast=True` uses the public oEmbed endpoint for an instant response without extended stats. |
| `transcribe_video` | `url` (string), `languages` (list[str], optional), `with_timestamps` (bool, default: False), `model_size` (string, optional), `keep_audio` (bool, default: False), `include_ocr` (bool, default: True) | Transcribes spoken audio locally with Whisper and extracts on-screen text from video keyframes or photo-carousel slides with Tesseract OCR. Returns combined `text`, separate `audio_text` and `ocr_text`, `ocr_segments`, and `source` (`whisper`, `whisper+ocr`, `ocr_video`, `ocr_slides`). |
| `publish_video` | `url` (string), `slug` (string, optional), `include_transcript` (bool, default: True), `languages` (list[str], optional), `max_height` (int, default: 1080) | Downloads video and cover, optionally transcribes speech and on-screen text, and uploads files to your own storage. |
| `batch_video_info` | `urls` (list[str]), `fast` (bool, default: True) | Fetches metadata for up to 20 TikTok links concurrently. |

### Language handling

`languages` controls how Whisper and Tesseract OCR are invoked:

| Value | Behaviour |
| :--- | :--- |
| `["ru"]` | Locks Whisper to Russian and prioritizes Russian + English in Tesseract OCR |
| `["ru", "en"]` | Runs a single multilingual pass (`multilingual=True`) covering code-switched speech and bilingual `rus+eng` OCR |
| `None` | Whisper auto-detects spoken language (`detected_language`, `language_probability`); OCR defaults to `rus+eng` |

Maximum 3 languages per call.

---

## Installation and Setup

### Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- `ffmpeg` (required for audio extraction, transcoding, and video keyframe extraction)
- `tesseract-ocr` with Russian and English language packs (required for screenshot and photo-carousel OCR)

```bash
# macOS
brew install ffmpeg tesseract tesseract-lang

# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
```

### 1. Clone repository

```bash
git clone https://github.com/bledny1099/tiktok-mcp.git
cd tiktok-mcp
```

### 2. Configure environment (optional)

```bash
cp .env.example .env
```

Edit `.env` to configure proxy, Whisper model, OCR languages, or media storage options if needed:

```bash
TIKTOK_MCP_TRANSPORT=http
TIKTOK_MCP_PORT=8770
TIKTOK_MCP_WHISPER_MODEL=large-v3-turbo
TIKTOK_MCP_OCR_LANGUAGES=rus+eng
TIKTOK_MCP_PROXY=http://127.0.0.1:10809
```

### 3. Pre-download the Whisper model (recommended)

Pre-downloading prevents the first transcription call from waiting on model weights:

```bash
uv run python download_model.py
```

### 4. Run server

**Option A: Local stdio (for IDEs and Desktop clients)**

```bash
uv run python server.py
```

**Option B: HTTP or SSE daemon (for remote server deployments)**

```bash
export TIKTOK_MCP_TRANSPORT=http
uv run python server.py
```

The HTTP transport binds to `127.0.0.1:8770` by default. Do not change the host to `0.0.0.0` without reading [Security](#security) first, as the server does not implement built-in authentication.

---

## Configuration Reference

All configuration is via environment variables. Every variable has a working default.

### Transport

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TIKTOK_MCP_TRANSPORT` | `stdio` | `stdio` when the client launches the process, `http` or `sse` when running as a daemon |
| `TIKTOK_MCP_HOST` | `127.0.0.1` | Bind address for HTTP/SSE transport. Keep on loopback behind a reverse proxy |
| `TIKTOK_MCP_PORT` | `8770` | Bind port for HTTP/SSE transport |
| `TIKTOK_MCP_WORK_DIR` | system temp | Scratch directory for downloads and the Whisper model cache |

### Extraction

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TIKTOK_MCP_PROXY` | none | `http://user:pass@host:port` or `socks5://host:port` |
| `TIKTOK_MCP_COOKIES_FILE` | none | Path to a Netscape-format `cookies.txt` |
| `TIKTOK_MCP_COOKIES_BROWSER` | none | Alternative: `chrome`, `firefox`, `edge`, `safari` |

### Transcription & OCR

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TIKTOK_MCP_WHISPER_MODEL` | `large-v3-turbo` | `tiny`, `base`, `small`, `medium`, `large-v3`, `large-v3-turbo` |
| `TIKTOK_MCP_WHISPER_DEVICE` | `auto` | `cpu`, `cuda`, `auto` |
| `TIKTOK_MCP_WHISPER_COMPUTE` | `int8` | `int8` for CPU, `float16` for GPU |
| `TIKTOK_MCP_OCR_LANGUAGES` | `rus+eng` | Tesseract language packs used for keyframe and slide OCR |
| `TIKTOK_MCP_OCR_CONCURRENCY` | `4` | Maximum concurrent OCR jobs |
| `TIKTOK_MCP_MAX_TRANSCRIBE_SEC` | `600` | Videos longer than this are rejected before download |
| `TIKTOK_MCP_MAX_CONCURRENT_TRANSCRIBE` | `1` | Parallel transcriptions. Raising this multiplies peak RAM |
| `TIKTOK_MCP_MAX_CONCURRENT_META` | `4` | Parallel metadata fetches |

### Media publishing

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TIKTOK_MCP_UPLOAD_MODE` | `off` | `local`, `http`, or `off` |
| `TIKTOK_MCP_LOCAL_DIR` | none | Destination directory in `local` mode |
| `TIKTOK_MCP_UPLOAD_URL` | none | Receiving endpoint in `http` mode |
| `TIKTOK_MCP_UPLOAD_TOKEN` | none | Sent as `Authorization: Bearer ...` to that endpoint |
| `TIKTOK_MCP_PUBLIC_BASE_URL` | none | Prefix used to build the returned public URLs |

### Resource requirements

| Model | Approx. RAM | Speed on CPU |
| :--- | :--- | :--- |
| `large-v3-turbo` | ~4 GB | Roughly real time |
| `small` | ~1.5 GB | ~2x faster, lower accuracy on noisy audio |
| `base` | ~0.7 GB | Suitable for 1 GB VPS instances |

`publish_video` additionally needs free disk space for the video, the extracted audio, and the merged output at the same time.

---

## Media Hosting and Downloads

When the AI model or user invokes `publish_video`, the server saves the media and returns publicly accessible URLs.

### Option 1: Custom Domain / Web Server (Self-Hosted)

Point a local directory served by Nginx or Caddy under your custom domain or CDN:

```bash
TIKTOK_MCP_UPLOAD_MODE=local
TIKTOK_MCP_LOCAL_DIR=/var/www/media
TIKTOK_MCP_PUBLIC_BASE_URL=https://media.yourdomain.com
```

Videos are saved into `/var/www/media` and returned as `https://media.yourdomain.com/<slug>.mp4`.

The service account running the server needs write access to that directory, and your web server needs read access:

```bash
sudo chown tiktokmcp:www-data /var/www/media
sudo chmod 0755 /var/www/media
```

### Option 2: GitHub Pages (Free Static Hosting)

If you don't have a dedicated web server or custom domain, host downloaded media on **GitHub Pages**:

1. Enable GitHub Pages on your repository (from `/docs` on `main`, or via a `gh-pages` branch).
2. Point the server at the local repository directory:
   ```bash
   TIKTOK_MCP_UPLOAD_MODE=local
   TIKTOK_MCP_LOCAL_DIR=/path/to/your-repo/docs/media
   TIKTOK_MCP_PUBLIC_BASE_URL=https://<username>.github.io/<repo>/media
   ```
3. Commit and push the downloaded files.

> GitHub Pages is public to the entire internet and has a soft 1 GB repository limit. Do not use it for anything you wouldn't publish deliberately.

### Option 3: Remote HTTP Upload Endpoint

In `http` mode the server sends a `multipart/form-data` POST to `TIKTOK_MCP_UPLOAD_URL`:

| Field | Content |
| :--- | :--- |
| `file` | Video bytes (`video/mp4`) or cover bytes (`image/jpeg` \| `image/webp`) |
| `meta` | JSON string: `id`, `title`, `description`, `hashtags`, `links`, `author`, `duration_sec`, `published_at`, `stats`, `music`, `transcript`, `audio_transcript`, `ocr_transcript` |

Header: `Authorization: Bearer <TIKTOK_MCP_UPLOAD_TOKEN>`.

Expected response: `200` with `{"url": "https://media.yourdomain.com/<file>"}`. If the body isn't JSON, the URL is assembled from `TIKTOK_MCP_PUBLIC_BASE_URL` plus the filename.

Minimal receiver:

```python
from fastapi import FastAPI, Depends, File, Form, UploadFile, HTTPException, Header
from pathlib import Path
import json, os

app = FastAPI()
MEDIA = Path("/var/www/media")

def auth(authorization: str = Header("")):
    if authorization != f"Bearer {os.environ['UPLOAD_TOKEN']}":
        raise HTTPException(401)

@app.post("/api/upload", dependencies=[Depends(auth)])
async def upload(file: UploadFile = File(...), meta: str = Form("{}")):
    MEDIA.mkdir(parents=True, exist_ok=True)
    dest = MEDIA / Path(file.filename).name      # strip any client-supplied path
    dest.write_bytes(await file.read())
    dest.with_suffix(dest.suffix + ".json").write_text(
        json.dumps(json.loads(meta), ensure_ascii=False, indent=2)
    )
    return {"url": f"https://media.yourdomain.com/{dest.name}"}
```

---

## Security

**The server implements no authentication of its own.** Every MCP client that can reach the port can invoke every tool. Authentication, TLS, and rate limiting are the reverse proxy's job.

An exposed instance lets strangers run Whisper on your CPU, pull arbitrary TikTok URLs through your IP, and write files onto your domain if `TIKTOK_MCP_UPLOAD_MODE` is enabled.

### Rules

1. Keep `TIKTOK_MCP_HOST=127.0.0.1`. Never publish port `8770` directly to the internet, and in Docker always map it as `-p 127.0.0.1:8770:8770`.
2. Terminate TLS and check a bearer token at the reverse proxy.
3. Generate the token with `openssl rand -hex 32`. Store it outside the repository.
4. Keep `.env` and `cookies.txt` out of version control (both are in `.gitignore`).
5. Rotate the token if it ever appears in a chat log, screenshot, or issue.

### Nginx

```nginx
map $http_authorization $mcp_ok {
    default                     0;
    "Bearer YOUR_MCP_TOKEN"     1;
}

server {
    listen 443 ssl http2;
    server_name mcp.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/mcp.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.yourdomain.com/privkey.pem;

    client_max_body_size 200m;

    location /mcp {
        if ($mcp_ok = 0) { return 401; }

        proxy_pass http://127.0.0.1:8770;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;

        # streaming responses must not be buffered
        proxy_buffering off;
        proxy_cache off;

        # transcription of long videos takes minutes
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
}
```

`$connection_upgrade` comes from the standard map in `http {}`:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

### Caddy

```caddyfile
mcp.yourdomain.com {
    @unauthorized not header Authorization "Bearer YOUR_MCP_TOKEN"
    respond @unauthorized 401

    reverse_proxy 127.0.0.1:8770 {
        flush_interval -1
        transport http {
            read_timeout 900s
        }
    }
}
```

### Firewall

```bash
sudo ufw allow 80,443/tcp
sudo ufw deny 8770
```

---

## Production Deployment

### Docker

```bash
docker build -t tiktok-mcp .

docker run -d --name tiktok-mcp \
  --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8770:8770 \
  -v tiktok-mcp-data:/var/lib/tiktok-mcp \
  tiktok-mcp
```

The named volume persists the Whisper model cache across container rebuilds so weights do not re-download on restart.

### systemd

`/etc/systemd/system/tiktok-mcp.service`:

```ini
[Unit]
Description=TikTok MCP Server
After=network-online.target
Wants=network-online.target

[Service]
User=tiktokmcp
WorkingDirectory=/opt/tiktok-mcp
EnvironmentFile=/etc/tiktok-mcp.env
ExecStart=/opt/tiktok-mcp/.venv/bin/python server.py
Restart=always
RestartSec=5
StartLimitIntervalSec=0

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/tiktok-mcp /var/www/media

MemoryMax=4G
OOMPolicy=continue

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tiktok-mcp
sudo systemctl status tiktok-mcp
```

`StartLimitIntervalSec=0` prevents systemd from giving up after repeated crashes.

Store the env file with restricted permissions:

```bash
sudo chown root:tiktokmcp /etc/tiktok-mcp.env
sudo chmod 0640 /etc/tiktok-mcp.env
```

### Keeping yt-dlp current

TikTok changes its page structure regularly. Schedule a weekly update:

```bash
/opt/tiktok-mcp/.venv/bin/pip install -U yt-dlp && systemctl restart tiktok-mcp
```

### Verifying the deployment

```bash
# 1. No token -> 401
curl -s -o /dev/null -w '%{http_code}\n' https://mcp.yourdomain.com/mcp

# 2. Handshake with token
curl -sN https://mcp.yourdomain.com/mcp \
  -H 'Authorization: Bearer YOUR_MCP_TOKEN' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'

# 3. Survives a reboot
sudo reboot && systemctl is-enabled tiktok-mcp && systemctl is-active tiktok-mcp
```

---

## Client Configuration

### 1. Claude Mobile (iOS / Android) & Claude Web

1. Open Claude on your phone or in the browser.
2. Go to **Settings** -> **Connectors**.
3. Click **Add custom connector**.
4. Enter the details:
   - **Name**: `TikTok`
   - **URL**: `https://mcp.yourdomain.com/mcp`
   - **Authentication**: Select **No sign-in**
   - Click **+ Add header**:
     - Key: `Authorization`
     - Value: `Bearer <YOUR_MCP_TOKEN>`
5. Click **Add connector**. The TikTok tools appear in your chat on both phone and web.

---

### 2. Cursor, Antigravity IDE, Windsurf, etc.

**Editor Settings UI:**
- Open **Settings** -> **Features** (or **Tools**) -> **MCP Servers** -> **Add New MCP Server**
- **Type**: `stdio`
- **Command**: `uv`
- **Args**: `run --directory /absolute/path/to/tiktok-mcp python server.py`

**Manual config (`mcp_config.json`)** for local stdio:

```json
{
  "mcpServers": {
    "tiktok": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/tiktok-mcp",
        "python",
        "server.py"
      ],
      "env": {
        "TIKTOK_MCP_PROXY": "http://127.0.0.1:10809"
      }
    }
  }
}
```

Remote Streamable HTTP:

```json
{
  "mcpServers": {
    "tiktok": {
      "url": "https://mcp.yourdomain.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

---

### 3. Claude Desktop

Configure the server in `claude_desktop_config.json`:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "tiktok": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/tiktok-mcp",
        "python",
        "server.py"
      ]
    }
  }
}
```

> **Note for Windows users:** Use forward slashes (e.g. `C:/Users/username/tiktok-mcp`) or escaped backslashes (`C:\\Users\\username\\tiktok-mcp`) in the `--directory` argument.

---

## Usage Example

```text
User: "Summarize this TikTok video and give me a full transcript: https://vt.tiktok.com/ZSqa7P1oy/"
```

The model calls:

1. `get_video_info(url="https://vt.tiktok.com/ZSqa7P1oy/")`
2. `transcribe_video(url="https://vt.tiktok.com/ZSqa7P1oy/", with_timestamps=True)`

The model receives the spoken transcript, any on-screen screenshot or slide OCR text, metadata, and author details.

---

## Troubleshooting

| Symptom | Cause and fix |
| :--- | :--- |
| `Unable to extract webpage video data` / `Fresh cookies are needed` | TikTok's JS challenge triggered and all fallbacks failed. Export cookies from a logged-in browser in Netscape format and set `TIKTOK_MCP_COOKIES_FILE` |
| `get_video_info` returns a `warning` field about oEmbed | `yt-dlp` and fallback scrapers failed, so the minimal oEmbed fallback was used |
| Metadata worked yesterday, fails today | Update `yt-dlp` first: `uv pip install -U yt-dlp` |
| Client disconnects mid-transcription | Raise `proxy_read_timeout` on the proxy, or lower `TIKTOK_MCP_MAX_TRANSCRIBE_SEC` so long videos are rejected up front |
| Process killed during transcription | Out of memory. Use a smaller `TIKTOK_MCP_WHISPER_MODEL` or keep `TIKTOK_MCP_MAX_CONCURRENT_TRANSCRIBE=1` |
| `publish_video` raises `Permission denied` | The service account can't write to `TIKTOK_MCP_LOCAL_DIR`. Check ownership and, under systemd, that the path is listed in `ReadWritePaths` |
| `ffmpeg not found` or `tesseract not found` | Install `ffmpeg` and `tesseract-ocr` (`tesseract-ocr-rus`, `tesseract-ocr-eng`) via your OS package manager |
| Empty transcript on a video with speech | Music-only or very noisy audio. Try an explicit `languages` value, or a larger `model_size` |
| Region-locked video | Route through `TIKTOK_MCP_PROXY` |

Work directories named `audio-*` and `publish-*` under `TIKTOK_MCP_WORK_DIR` are cleaned up automatically, though a `SIGKILL` (including an OOM kill) can leave temp directories behind.

---

## Legal Notice

Scraping public pages conflicts with TikTok's Terms of Service, and the videos themselves are protected by their authors' copyright. Republishing someone else's video on your own domain without permission carries DMCA risk.

Safe uses: your own content, content you are licensed to use, or storing only transcripts and metadata for analysis rather than the video itself. You are responsible for how you use this tool.

---

## License

[MIT](LICENSE)
