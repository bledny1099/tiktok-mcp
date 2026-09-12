"""TikTok MCP server.

Metadata and media extraction via yt-dlp public page scraping (no TikTok Developer API / OAuth required).
Local speech-to-text transcription powered by faster-whisper.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

mcp = FastMCP(
    "tiktok",
    instructions=(
        "TikTok media processing without official API: extract video metadata "
        "(title, description, cover thumbnail, author, statistics), transcribe speech to text "
        "locally with Whisper, and publish media to a custom domain or storage."
    ),
)

# --------------------------------------------------------------------------- #
# Configuration (environment variables)
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    work_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("TIKTOK_MCP_WORK_DIR", tempfile.gettempdir()) or tempfile.gettempdir()
        )
        / "tiktok-mcp"
    )
    # cookies: path to cookies.txt OR browser name (chrome/firefox/edge/safari)
    cookies_file: str | None = os.getenv("TIKTOK_MCP_COOKIES_FILE") or None
    cookies_browser: str | None = os.getenv("TIKTOK_MCP_COOKIES_BROWSER") or None
    proxy: str | None = os.getenv("TIKTOK_MCP_PROXY") or None

    # whisper
    whisper_model: str = os.getenv("TIKTOK_MCP_WHISPER_MODEL", "large-v3-turbo")
    whisper_device: str = os.getenv("TIKTOK_MCP_WHISPER_DEVICE", "auto")
    whisper_compute: str = os.getenv("TIKTOK_MCP_WHISPER_COMPUTE", "int8")

    # publishing
    upload_mode: Literal["http", "local", "off"] = os.getenv("TIKTOK_MCP_UPLOAD_MODE", "off")  # type: ignore[assignment]
    upload_url: str | None = os.getenv("TIKTOK_MCP_UPLOAD_URL") or None
    upload_token: str | None = os.getenv("TIKTOK_MCP_UPLOAD_TOKEN") or None
    local_dir: str | None = os.getenv("TIKTOK_MCP_LOCAL_DIR") or None
    public_base_url: str = os.getenv("TIKTOK_MCP_PUBLIC_BASE_URL", "")

    # concurrency limits
    max_transcribe_sec: int = int(os.getenv("TIKTOK_MCP_MAX_TRANSCRIBE_SEC", "600"))
    max_concurrent_transcribe: int = int(os.getenv("TIKTOK_MCP_MAX_CONCURRENT_TRANSCRIBE", "1"))
    max_concurrent_meta: int = int(os.getenv("TIKTOK_MCP_MAX_CONCURRENT_META", "4"))


CFG = Config()
CFG.work_dir.mkdir(parents=True, exist_ok=True)

_TRANSCRIBE_SEM = asyncio.Semaphore(CFG.max_concurrent_transcribe)
_META_SEM = asyncio.Semaphore(CFG.max_concurrent_meta)

_URL_RE = re.compile(r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/\S+", re.I)
_HASHTAG_RE = re.compile(r"#(\w+)")
_LINK_RE = re.compile(r"https?://[^\s<>\"]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _validate_url(url: str) -> str:
    url = url.strip()
    if not _URL_RE.match(url):
        raise ValueError(f"Invalid TikTok URL: {url!r}")
    return url


def _slugify(text: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")[:60]
    return slug or fallback


# --------------------------------------------------------------------------- #
# yt-dlp & fallback extraction
# --------------------------------------------------------------------------- #


def _ydl_opts(**extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
    }
    if CFG.cookies_file:
        opts["cookiefile"] = CFG.cookies_file
    elif CFG.cookies_browser:
        opts["cookiesfrombrowser"] = (CFG.cookies_browser,)
    if CFG.proxy:
        opts["proxy"] = CFG.proxy
    opts.update(extra)
    return opts


def _extract_fallback(url: str, *, download: bool = False, **extra: Any) -> dict[str, Any]:
    """Fallback scraper via public tikwm API in case yt-dlp encounters anti-bot challenges."""
    with httpx.Client(proxy=CFG.proxy, timeout=30.0, follow_redirects=True) as client:
        resp = client.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise RuntimeError(f"Tikwm fallback error: {payload.get('msg', 'unknown error')}")

        def _tikwm_url(u: str | None) -> str | None:
            if not u:
                return None
            if u.startswith("http://") or u.startswith("https://"):
                return u
            return "https://www.tikwm.com" + u

        d = payload["data"]
        info: dict[str, Any] = {
            "id": str(d.get("id")),
            "title": d.get("title"),
            "description": d.get("title"),
            "uploader": d.get("author", {}).get("nickname"),
            "uploader_id": d.get("author", {}).get("unique_id"),
            "uploader_url": f"https://www.tiktok.com/@{d.get('author', {}).get('unique_id')}",
            "thumbnail": _tikwm_url(d.get("cover")),
            "duration": d.get("duration"),
            "timestamp": d.get("create_time"),
            "view_count": d.get("play_count"),
            "like_count": d.get("digg_count"),
            "comment_count": d.get("comment_count"),
            "repost_count": d.get("share_count"),
            "track": d.get("music_info", {}).get("title"),
            "artist": d.get("music_info", {}).get("author"),
            "webpage_url": url,
            "original_url": url,
            "source": "fallback_api",
        }

        if download:
            outtmpl = extra.get("outtmpl")
            video_url = _tikwm_url(d.get("play") or d.get("wmplay") or d.get("hdplay"))
            audio_url = _tikwm_url(d.get("music")) or video_url

            postprocessors = extra.get("postprocessors", [])
            needs_wav = any(p.get("preferredcodec") == "wav" for p in postprocessors)

            if outtmpl:
                resolved = outtmpl.replace("%(id)s", info["id"])
                dl_url = audio_url if needs_wav else video_url
                ext = "mp3" if (needs_wav and d.get("music")) else "mp4"
                target_file = Path(resolved.replace("%(ext)s", ext))

                r = client.get(dl_url, headers={"Referer": "https://www.tiktok.com/"})
                r.raise_for_status()
                target_file.write_bytes(r.content)

                if needs_wav:
                    wav_file = Path(resolved.replace("%(ext)s", "wav"))
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", str(target_file), "-ar", "16000", "-ac", "1", str(wav_file)],
                        check=True,
                        capture_output=True,
                    )
                    if target_file != wav_file and target_file.exists():
                        target_file.unlink()
                elif extra.get("merge_output_format") == "mp4":
                    mp4_file = Path(resolved.replace("%(ext)s", "mp4"))
                    if target_file != mp4_file:
                        target_file.rename(mp4_file)

        return info


def _extract(url: str, *, download: bool = False, **extra: Any) -> dict[str, Any]:
    try:
        with YoutubeDL(_ydl_opts(**extra)) as ydl:
            info = ydl.extract_info(url, download=download)
        if info and info.get("_type") == "playlist":
            entries = [e for e in info.get("entries") or [] if e]
            if not entries:
                raise ValueError("No videos found at the provided URL")
            info = entries[0]
        return info or {}
    except Exception as exc:
        try:
            return _extract_fallback(url, download=download, **extra)
        except Exception:
            raise exc


def _iso(ts: Any) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _best_thumbnail(info: dict[str, Any]) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return None
    thumbs.sort(key=lambda t: (t.get("preference") or 0, t.get("width") or 0))
    return thumbs[-1]["url"]


def _pack_meta(info: dict[str, Any]) -> dict[str, Any]:
    description = info.get("description") or ""
    raw_title = info.get("title") or description
    # TikTok often puts full description in title — truncate to first line / 100 chars
    title = raw_title.split("\n", 1)[0].strip()
    if len(title) > 100:
        title = title[:97].rstrip() + "…"
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "title": title or None,
        "description": description,
        "hashtags": _HASHTAG_RE.findall(description),
        "links": _LINK_RE.findall(description),
        "thumbnail": _best_thumbnail(info),
        "author": {
            "name": info.get("uploader") or info.get("creator"),
            "handle": info.get("uploader_id"),
            "url": info.get("uploader_url"),
        },
        "duration_sec": info.get("duration"),
        "published_at": _iso(info.get("timestamp")),
        "stats": {
            "views": info.get("view_count"),
            "likes": info.get("like_count"),
            "comments": info.get("comment_count"),
            "shares": info.get("repost_count"),
        },
        "music": {"track": info.get("track"), "artist": info.get("artist")},
        "source": info.get("source", "yt-dlp"),
    }


async def _oembed(url: str) -> dict[str, Any]:
    """Lightweight public oEmbed endpoint: title, author, and thumbnail only, without API keys."""
    async with httpx.AsyncClient(proxy=CFG.proxy, timeout=10, follow_redirects=True) as client:
        r = await client.get("https://www.tiktok.com/oembed", params={"url": url})
        r.raise_for_status()
        data = r.json()
    return {
        "id": data.get("embed_product_id"),
        "url": url,
        "title": data.get("title"),
        "description": data.get("title"),
        "hashtags": _HASHTAG_RE.findall(data.get("title") or ""),
        "links": _LINK_RE.findall(data.get("title") or ""),
        "thumbnail": data.get("thumbnail_url"),
        "author": {"name": data.get("author_name"), "url": data.get("author_url")},
        "source": "oembed",
    }


# --------------------------------------------------------------------------- #
# Whisper
# --------------------------------------------------------------------------- #

_model_cache: dict[tuple[str, str, str], Any] = {}


def _get_model(size: str | None = None):
    from faster_whisper import WhisperModel

    key = (size or CFG.whisper_model, CFG.whisper_device, CFG.whisper_compute)
    if key not in _model_cache:
        _model_cache[key] = WhisperModel(
            key[0], device=key[1], compute_type=key[2], download_root=str(CFG.work_dir / "models")
        )
    return _model_cache[key]


def _filter_kwargs(fn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Discards parameters unsupported by the installed version of faster-whisper."""
    allowed = set(inspect.signature(fn).parameters)
    return {k: v for k, v in kwargs.items() if k in allowed}


def _run_whisper(
    audio_path: Path,
    languages: list[str] | None,
    model_size: str | None,
    with_timestamps: bool,
) -> dict[str, Any]:
    model = _get_model(model_size)
    kwargs: dict[str, Any] = {
        "beam_size": 5,
        "vad_filter": True,
        "condition_on_previous_text": False,
    }
    if languages and len(languages) == 1:
        kwargs["language"] = languages[0]
    elif languages and len(languages) > 1:
        # Single multilingual model handles all selected languages without switching
        kwargs["multilingual"] = True
    kwargs = _filter_kwargs(model.transcribe, kwargs)

    segments, info = model.transcribe(str(audio_path), **kwargs)
    parts, timed = [], []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        parts.append(text)
        if with_timestamps:
            timed.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})

    result: dict[str, Any] = {
        "text": " ".join(parts),
        "detected_language": getattr(info, "language", None),
        "language_probability": round(getattr(info, "language_probability", 0.0) or 0.0, 3),
        "audio_duration_sec": round(getattr(info, "duration", 0.0) or 0.0, 2),
        "model": model_size or CFG.whisper_model,
    }
    if with_timestamps:
        result["segments"] = timed
    return result


# --------------------------------------------------------------------------- #
# Upload to custom domain or storage
# --------------------------------------------------------------------------- #


async def _upload(path: Path, content_type: str, meta: dict[str, Any]) -> str:
    if CFG.upload_mode == "off":
        raise RuntimeError(
            "Publishing is disabled. Set TIKTOK_MCP_UPLOAD_MODE=http|local and related environment variables."
        )
    if CFG.upload_mode == "local":
        if not CFG.local_dir:
            raise RuntimeError("TIKTOK_MCP_LOCAL_DIR is not configured")
        dest_dir = Path(CFG.local_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest_dir / path.name)
        if not CFG.public_base_url:
            return str(dest_dir / path.name)
        return f"{CFG.public_base_url.rstrip('/')}/{path.name}"

    if not CFG.upload_url:
        raise RuntimeError("TIKTOK_MCP_UPLOAD_URL is not configured")
    headers = {"Authorization": f"Bearer {CFG.upload_token}"} if CFG.upload_token else {}
    async with httpx.AsyncClient(timeout=600) as client:
        with path.open("rb") as fh:
            resp = await client.post(
                CFG.upload_url,
                headers=headers,
                files={"file": (path.name, fh, content_type)},
                data={"meta": json.dumps(meta, ensure_ascii=False)},
            )
    resp.raise_for_status()
    try:
        return resp.json().get("url") or f"{CFG.public_base_url.rstrip('/')}/{path.name}"
    except ValueError:
        return f"{CFG.public_base_url.rstrip('/')}/{path.name}"


# --------------------------------------------------------------------------- #
# MCP Tools
# --------------------------------------------------------------------------- #


async def _video_info(url: str, fast: bool = False) -> dict[str, Any]:
    url = _validate_url(url)
    if fast:
        try:
            return await _oembed(url)
        except Exception:
            pass
    try:
        async with _META_SEM:
            info = await asyncio.to_thread(_extract, url)
        return _pack_meta(info)
    except DownloadError as exc:
        # Common cause: TikTok requires fresh cookies; return oEmbed fallback
        try:
            fallback = await _oembed(url)
            fallback["warning"] = f"yt-dlp failed to parse page ({exc}); returning oEmbed fallback"
            return fallback
        except Exception:
            raise exc


@mcp.tool
async def get_video_info(url: str, fast: bool = False) -> dict[str, Any]:
    """Returns title, description, cover thumbnail, author, and statistics for a TikTok video.

    Args:
        url: TikTok video URL (full or shortened vm./vt. link).
        fast: When True, uses lightweight oEmbed (instant response, omitting extended statistics).
    """
    return await _video_info(url, fast)


async def _transcribe(
    url: str,
    languages: list[str] | None = None,
    with_timestamps: bool = False,
    model_size: str | None = None,
    keep_audio: bool = False,
) -> dict[str, Any]:
    url = _validate_url(url)
    if languages and len(languages) > 3:
        raise ValueError("A maximum of 3 languages can be specified at once")

    # Guard against excessively long videos before downloading to prevent client timeouts
    probe = await asyncio.to_thread(_extract, url)
    duration = probe.get("duration") or 0
    if duration and duration > CFG.max_transcribe_sec:
        raise ValueError(
            f"Video duration is {int(duration)}s, exceeding limit of {CFG.max_transcribe_sec}s. "
            "Increase TIKTOK_MCP_MAX_TRANSCRIBE_SEC or process offline."
        )

    async with _TRANSCRIBE_SEM:
        tmp_dir = Path(tempfile.mkdtemp(dir=CFG.work_dir, prefix="audio-"))
        try:
            info = await asyncio.to_thread(
                _extract,
                url,
                download=True,
                format="bestaudio/best",
                outtmpl=str(tmp_dir / "%(id)s.%(ext)s"),
                postprocessors=[
                    {"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "0"}
                ],
                postprocessor_args={"extractaudio": ["-ar", "16000", "-ac", "1"]},
            )
            audio = next((p for p in tmp_dir.glob("*.wav")), None) or next(tmp_dir.iterdir())
            result = await asyncio.to_thread(
                _run_whisper, audio, languages, model_size, with_timestamps
            )
            result["video"] = {
                "id": info.get("id"),
                "title": info.get("title"),
                "author": info.get("uploader_id"),
                "url": info.get("webpage_url") or url,
            }
            if keep_audio:
                result["audio_path"] = str(audio)
            return result
        finally:
            if not keep_audio:
                shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool
async def transcribe_video(
    url: str,
    languages: list[str] | None = None,
    with_timestamps: bool = False,
    model_size: str | None = None,
    keep_audio: bool = False,
) -> dict[str, Any]:
    """Downloads audio from a TikTok video and transcribes speech to text locally using Whisper.

    Args:
        url: TikTok video URL.
        languages: 1-3 language codes (e.g., ['en', 'es']). One locks the model;
            multiple uses a single multilingual model; None enables auto-detection.
        with_timestamps: Return word/segment breakdown with start and end timestamps.
        model_size: Whisper model override (tiny/base/small/medium/large-v3/large-v3-turbo).
        keep_audio: Retain downloaded audio file on disk instead of removing it.
    """
    return await _transcribe(url, languages, with_timestamps, model_size, keep_audio)


@mcp.tool
async def publish_video(
    url: str,
    slug: str | None = None,
    include_transcript: bool = True,
    languages: list[str] | None = None,
    max_height: int = 1080,
) -> dict[str, Any]:
    """Downloads video and cover thumbnail, uploads them to custom domain storage, and returns public URLs.

    Args:
        url: TikTok video URL.
        slug: Output filename without extension (defaults to sanitized video title).
        include_transcript: Transcribe speech and attach text to video metadata.
        languages: Language codes for transcription (see transcribe_video).
        max_height: Maximum video resolution height (e.g. 720, 1080).
    """
    url = _validate_url(url)
    tmp_dir = Path(tempfile.mkdtemp(dir=CFG.work_dir, prefix="publish-"))
    try:
        info = await asyncio.to_thread(
            _extract,
            url,
            download=True,
            format=f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
            merge_output_format="mp4",
            outtmpl=str(tmp_dir / "video.%(ext)s"),
        )
        meta = _pack_meta(info)
        name = _slugify(slug or meta["title"] or "", fallback=str(meta["id"] or "tiktok"))

        video_file = next(tmp_dir.glob("video.*"))
        target = tmp_dir / f"{name}{video_file.suffix}"
        video_file.rename(target)

        if include_transcript:
            transcript = await _transcribe(url, languages=languages)
            meta["transcript"] = transcript["text"]
            meta["transcript_language"] = transcript["detected_language"]

        meta["video_url"] = await _upload(target, "video/mp4", meta)

        if meta.get("thumbnail"):
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(meta["thumbnail"])
            if r.status_code == 200:
                ext = ".webp" if "webp" in r.headers.get("content-type", "") else ".jpg"
                cover = tmp_dir / f"{name}-cover{ext}"
                cover.write_bytes(r.content)
                meta["cover_url"] = await _upload(cover, r.headers.get("content-type", "image/jpeg"), meta)

        meta["published"] = True
        return meta
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool
async def batch_video_info(urls: list[str], fast: bool = True) -> list[dict[str, Any]]:
    """Fetches metadata for up to 20 TikTok video URLs concurrently."""
    if len(urls) > 20:
        raise ValueError("Maximum 20 URLs allowed per batch request")
    results = await asyncio.gather(
        *(_video_info(u, fast) for u in urls),
        return_exceptions=True,
    )
    return [
        {"url": u, "error": str(r)} if isinstance(r, Exception) else r
        for u, r in zip(urls, results)
    ]


def main() -> None:
    """stdio transport when an MCP client launches the process; http when running as a daemon."""
    transport = os.getenv("TIKTOK_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport="http",
            host=os.getenv("TIKTOK_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("TIKTOK_MCP_PORT", "8770")),
        )


if __name__ == "__main__":
    main()
