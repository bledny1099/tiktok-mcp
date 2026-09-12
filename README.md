<p align="center">
  <img src="assets/logo.png" alt="TikTok Logo" width="240" />
</p>

<h1 align="center">TikTok MCP Server</h1>

<p align="center">
  Model Context Protocol server for extracting TikTok video metadata, transcribing speech with Faster-Whisper, and automating media workflows.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Protocol-MCP-8A2BE2?style=flat" alt="MCP" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat" alt="License" />
</p>

<p align="center">
  <a href="#overview">Overview</a> •
  <a href="#available-tools">Available Tools</a> •
  <a href="#installation-and-setup">Setup</a> •
  <a href="#media-hosting-and-downloads">Media Hosting</a> •
  <a href="#client-configuration">Configuration</a> •
  <a href="#license">License</a>
</p>

---

## Overview

This MCP server connects your AI assistant (Claude Desktop, Antigravity IDE, Cursor, etc.) directly to [TikTok](https://tiktok.com). It enables you to pull video descriptions, author statistics, tags, media streams, and speech-to-text transcriptions directly into your workspace.

### Key capabilities

- **Reliable extraction with fallback**: Extracts video metadata, view counts, and direct audio/video streams via yt-dlp, with automated API fallback for region-locked or challenge-restricted content.
- **Local Speech-to-Text**: Fast, high-accuracy speech transcription powered by `faster-whisper` (`large-v3-turbo` with int8 quantization support).
- **Outbound proxy support**: Native HTTP & SOCKS5 proxy routing (`TIKTOK_MCP_PROXY`) to handle cross-border blocks and georestricted videos.
- **Custom domain publishing**: Download media and host video/cover assets directly on your own domain or CDN.

---

## Available Tools

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `get_video_info` | `url` (string), `fast` (bool, default: False) | Returns video title, description, author, stats, and cover image. |
| `transcribe_video` | `url` (string), `languages` (list[str], optional), `with_timestamps` (bool, default: False), `model_size` (string, optional), `keep_audio` (bool, default: False) | Downloads audio and transcribes speech to text locally using Whisper. |
| `publish_video` | `url` (string), `slug` (string, optional), `include_transcript` (bool, default: True), `languages` (list[str], optional), `max_height` (int, default: 1080) | Downloads video, extracts transcript, and uploads files to custom domain storage. |
| `batch_video_info` | `urls` (list[str]), `fast` (bool, default: True) | Fetches metadata for up to 20 TikTok links concurrently. |

---

## Installation and Setup

### Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- `ffmpeg` (required for audio extraction and transcoding)

### 1. Clone repository

```bash
git clone https://github.com/bledny1099/tiktok-mcp.git
cd tiktok-mcp
```

### 2. Configure environment (optional)

```bash
cp .env.example .env
```

Edit `.env` to configure proxy, whisper model, or media storage options if needed:
```bash
TIKTOK_MCP_TRANSPORT=http
TIKTOK_MCP_PORT=8770
TIKTOK_MCP_WHISPER_MODEL=large-v3-turbo
TIKTOK_MCP_PROXY=http://127.0.0.1:10809
```

### 3. Run server

**Option A: Local stdio (for IDEs and Desktop clients)**
```bash
uv run tiktok-mcp
```

**Option B: HTTP / SSE daemon (for remote server deployments)**
```bash
export TIKTOK_MCP_TRANSPORT=http
uv run tiktok-mcp
```

---

## Media Hosting and Downloads

When the AI model or user invokes `publish_video` to download and host video clips alongside speech transcripts, the server saves the media and returns publicly accessible URLs.

To make downloaded videos and transcripts accessible to your AI assistant:

### Option 1: Custom Domain / Web Server (Self-Hosted)

Point a local directory served by Nginx or Caddy under your custom domain or CDN:

```bash
TIKTOK_MCP_UPLOAD_MODE=local
TIKTOK_MCP_LOCAL_DIR=/var/www/media
TIKTOK_MCP_PUBLIC_BASE_URL=https://media.yourdomain.com
```

Videos will be saved directly into `/var/www/media` and returned as `https://media.yourdomain.com/<slug>.mp4`.

### Option 2: GitHub Pages (Free Static Hosting)

If you don't have a dedicated web server or custom domain, you can host downloaded media files directly on **GitHub Pages**:

1. Enable GitHub Pages on your repository (e.g. from `/docs` folder on `main` or via a dedicated `gh-pages` branch).
2. Set `TIKTOK_MCP_LOCAL_DIR` to the local repository directory:
   ```bash
   TIKTOK_MCP_UPLOAD_MODE=local
   TIKTOK_MCP_LOCAL_DIR=/path/to/your-repo/docs/media
   TIKTOK_MCP_PUBLIC_BASE_URL=https://<username>.github.io/<repo>/media
   ```
3. Commit and push downloaded files to GitHub. The AI assistant and users can stream and inspect the extracted videos directly via the GitHub Pages URL!

---

## Client Configuration

Choose the simplest setup method for your app or device:

### 1. Claude Mobile (iOS / Android) & Claude Web (Zero Files, UI Only)

In the mobile app or browser, **no JSON files or terminal commands are needed**:

1. Open Claude on your phone or in the web browser.
2. Go to **Settings** → **Connectors** (или **Настройки** → **Коннекторы**).
3. Click **Add custom connector** (**Добавить коннектор**).
4. Enter the connector details:
   - **Name**: `TikTok`
   - **URL**: `https://mcp.yourdomain.com/mcp`
   - **Authentication**: Select **No sign-in**
   - Click **+ Add header**:
     - Key: `Authorization`
     - Value: `Bearer <YOUR_MCP_TOKEN>`
5. Click **Add connector**. The TikTok tools will appear directly in your chat on both phone and web!

---

### 2. Cursor, Antigravity IDE, Windsurf, etc.

In AI IDEs (Cursor, Antigravity IDE, Windsurf), MCP servers can be added directly through the editor UI or configuration files. The setup is practically identical across all three:

**Automatic 1-Click Install in Cursor:**
```bash
uv run fastmcp install cursor tiktok_mcp.py
```

**Editor Settings UI (Cursor / Antigravity IDE / Windsurf):**
- Open **Settings** → **Features** (or **Tools**) → **MCP Servers** → **Add New MCP Server**
- **Type**: `stdio`
- **Command**: `uv`
- **Args**: `run --directory /absolute/path/to/tiktok-mcp tiktok-mcp`

**Manual Editor Config (`mcp_config.json`):**

*Local stdio:*
```json
{
  "mcpServers": {
    "tiktok": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/tiktok-mcp",
        "tiktok-mcp"
      ],
      "env": {
        "TIKTOK_MCP_PROXY": "http://127.0.0.1:10809"
      }
    }
  }
}
```

*Remote Streamable HTTP / SSE:*
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

Skip opening hidden configuration folders or editing JSON files manually:

**Automatic 1-Click Install via CLI:**
```bash
uv run fastmcp install claude-desktop tiktok_mcp.py
```
*FastMCP automatically locates your Claude Desktop settings and registers the server.*

*(Optional) Manual Config (`claude_desktop_config.json`):*
```json
{
  "mcpServers": {
    "tiktok": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/tiktok-mcp",
        "tiktok-mcp"
      ]
    }
  }
}
```

---

## Usage Example

Once configured, your AI assistant can interact with TikTok directly:

```text
User: "Summarize this TikTok video and give me a full transcript: https://vt.tiktok.com/ZSqa7P1oy/"
```

The model calls:
1. `get_video_info(url="https://vt.tiktok.com/ZSqa7P1oy/")`
2. `transcribe_video(url="https://vt.tiktok.com/ZSqa7P1oy/", with_timestamps=True)`

The model receives the exact speech transcription, metadata, and author details in Markdown.

---

## License

[MIT](LICENSE)
