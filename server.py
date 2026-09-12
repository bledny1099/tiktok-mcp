# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastmcp>=0.4.0",
#     "yt-dlp>=2024.0.0",
#     "faster-whisper>=1.0.0",
#     "httpx>=0.25.0",
# ]
# ///
"""Entry point for TikTok MCP server."""

from tiktok_mcp import main, mcp

if __name__ == "__main__":
    main()
