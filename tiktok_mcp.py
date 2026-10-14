"""TikTok MCP server.

Metadata and media extraction via yt-dlp public page scraping, tikwm API, and TikTok embed fallback
(no TikTok Developer API / OAuth required).
Local speech-to-text transcription powered by faster-whisper, plus automatic visual OCR
for videos containing screenshots/slides and TikTok Photo Mode carousels.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ocr import (
    extract_images_ocr,
    extract_video_ocr,
    is_ocr_available,
    merge_speech_and_ocr,
)

mcp = FastMCP(
    "tiktok",
    instructions=(
        "TikTok media processing without official API: extract video and photo-carousel metadata "
        "(title, description, cover thumbnail, author, statistics, slide URLs), transcribe speech "
        "to text locally with Whisper and on-screen screenshots/slides with Tesseract OCR, "
        "and publish media to a custom domain or storage."
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

_META_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_META_CACHE_TTL = 600.0
_META_CACHE_MAX = 128

_URL_RE = re.compile(r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/\S+", re.I)
_HASHTAG_RE = re.compile(r"#(\w+)")
_LINK_RE = re.compile(r"https?://[^\s<>\"]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Known Whisper hallucination phrases on background music or silence
_HALLUCINATION_RE = re.compile(
    r"^(?:"
    r"продолжение\s+следует\.?|"
    r"субтитры\s+(?:сделал|подогнал|создал|добавил)\b.*|"
    r"редактор\s+субтитров\b.*|"
    r"корректор\b.*|"
    r"перевод\s+и\s+озвучка\b.*|"
    r"subtitles?\s+by\b.*|"
    r"amara\.org.*|"
    r"thank\s+you\s+for\s+watching\.?|"
    r"thanks\s+for\s+watching\.?"
    r")$",
    re.IGNORECASE,
)


def _validate_url(url: str) -> str:
    url = url.strip()
    if url.isdigit() and len(url) >= 15:
        return f"https://www.tiktok.com/@tiktok/video/{url}"
    if not _URL_RE.match(url):
        raise ValueError(f"Invalid TikTok URL: {url!r}")
    return url


def _extract_id_from_url(url: str) -> str | None:
    m = re.search(r"/(?:video|photo)/(\d+)", url)
    if m:
        return m.group(1)
    digits = re.search(r"(\d{15,25})", url)
    return digits.group(1) if digits else None


def _slugify(text: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")[:60]
    return slug or fallback


def _store_meta_cache(url: str, info: dict[str, Any]) -> None:
    if len(_META_CACHE) >= _META_CACHE_MAX:
        _META_CACHE.pop(next(iter(_META_CACHE)))
    _META_CACHE[url] = (time.time(), copy.deepcopy(info))


def _get_meta_cache(url: str) -> dict[str, Any] | None:
    entry = _META_CACHE.get(url)
    if entry and (time.time() - entry[0]) < _META_CACHE_TTL:
        return copy.deepcopy(entry[1])
    return None


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
        raw_images = d.get("images") or []
        images = [_tikwm_url(img) for img in raw_images if img]
        images = [img for img in images if img]
        is_carousel = bool(images)
        carousel_items = [
            {"index": idx, "type": "image", "url": img_url, "thumbnail": img_url}
            for idx, img_url in enumerate(images, start=1)
        ]

        video_url = _tikwm_url(d.get("play") or d.get("wmplay") or d.get("hdplay"))
        audio_url = _tikwm_url(d.get("music")) or video_url

        info: dict[str, Any] = {
            "id": str(d.get("id")),
            "title": d.get("title"),
            "description": d.get("title"),
            "uploader": d.get("author", {}).get("nickname"),
            "uploader_id": d.get("author", {}).get("unique_id"),
            "uploader_url": f"https://www.tiktok.com/@{d.get('author', {}).get('unique_id')}",
            "thumbnail": _tikwm_url(d.get("cover")) or (images[0] if images else None),
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
            "direct_video_url": None if is_carousel else video_url,
            "direct_audio_url": audio_url,
            "is_carousel": is_carousel,
            "carousel_items": carousel_items,
            "media_type": "carousel" if is_carousel else "video",
            "source": "fallback_api",
        }

        if download:
            outtmpl = extra.get("outtmpl")
            postprocessors = extra.get("postprocessors", [])
            needs_wav = any(p.get("preferredcodec") == "wav" for p in postprocessors)

            if outtmpl:
                resolved = outtmpl.replace("%(id)s", info["id"])
                # Prefer downloading the MP4 video when available so both Whisper and visual OCR can run
                dl_url = (video_url or audio_url) if not is_carousel else audio_url
                if dl_url:
                    ext = "mp4" if (dl_url == video_url and not is_carousel) else "mp3"
                    target_file = Path(resolved.replace("%(ext)s", ext))

                    r = client.get(dl_url, headers={"Referer": "https://www.tiktok.com/"})
                    r.raise_for_status()
                    target_file.write_bytes(r.content)

                    if needs_wav:
                        wav_file = Path(resolved.replace("%(ext)s", "wav"))
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(target_file),
                                "-vn",
                                "-ar",
                                "16000",
                                "-ac",
                                "1",
                                str(wav_file),
                            ],
                            check=False,
                            capture_output=True,
                        )
                    elif extra.get("merge_output_format") == "mp4":
                        mp4_file = Path(resolved.replace("%(ext)s", "mp4"))
                        if target_file != mp4_file:
                            target_file.rename(mp4_file)

        return info


def _extract_embed_fallback(url: str, *, download: bool = False, **extra: Any) -> dict[str, Any]:
    """Second fallback via TikTok's anonymous embed v2 page (__FRONTITY_CONNECT_STATE__)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    }
    with httpx.Client(proxy=CFG.proxy, timeout=20.0, follow_redirects=True, headers=headers) as client:
        resolved_url = url
        video_id = _extract_id_from_url(resolved_url)
        if not video_id:
            resp_redir = client.get(url)
            resolved_url = str(resp_redir.url)
            video_id = _extract_id_from_url(resolved_url)
        if not video_id:
            raise RuntimeError(f"Could not resolve TikTok video ID from {url}")

        embed_resp = client.get(f"https://www.tiktok.com/embed/v2/{video_id}")
        embed_resp.raise_for_status()
        html = embed_resp.text

        pos = html.find("__FRONTITY_CONNECT_STATE__")
        if pos == -1:
            raise RuntimeError("TikTok embed page did not contain __FRONTITY_CONNECT_STATE__")

        tag_end = html.find(">", pos)
        script_end = html.find("</script>", tag_end)
        data = json.loads(html[tag_end + 1 : script_end])

        source = data.get("source", {})
        video_data = None
        for val in source.get("data", {}).values():
            if isinstance(val, dict) and "videoData" in val:
                video_data = val["videoData"]
                break
        if not video_data:
            raise RuntimeError(f"No videoData found in embed payload for {video_id}")

        item = video_data.get("itemInfos", {})
        author = video_data.get("authorInfos", {})
        music = video_data.get("musicInfos", {})
        image_post_info = video_data.get("imagePostInfo") or item.get("imagePostInfo")

        carousel_items: list[dict[str, Any]] = []
        if image_post_info and image_post_info.get("images"):
            for idx, img in enumerate(image_post_info.get("images", []), start=1):
                img_url = None
                if isinstance(img, dict):
                    for subk in ("display_image", "imageURL", "image", "thumbnail", "imageThumbnail"):
                        sub = img.get(subk)
                        if isinstance(sub, dict):
                            urls = sub.get("urlList") or sub.get("url_list") or sub.get("urls")
                            if urls and isinstance(urls, list) and urls[0]:
                                img_url = urls[0]
                                break
                        elif isinstance(sub, list) and sub:
                            img_url = sub[0]
                            break
                        elif isinstance(sub, str):
                            img_url = sub
                            break
                elif isinstance(img, str):
                    img_url = img
                if img_url:
                    carousel_items.append(
                        {"index": idx, "type": "image", "url": img_url, "thumbnail": img_url}
                    )

        is_carousel = bool(carousel_items)
        v_urls = item.get("video", {}).get("urls", [])
        m_urls = music.get("playUrl")
        if isinstance(m_urls, dict):
            m_urls = m_urls.get("urls", [])
        elif isinstance(m_urls, str):
            m_urls = [m_urls]
        elif not isinstance(m_urls, list):
            m_urls = []

        covers = item.get("covers", [])
        video_url = v_urls[0] if v_urls else None
        audio_url = m_urls[0] if m_urls else video_url
        uploader_id = author.get("uniqueId") or ""

        info: dict[str, Any] = {
            "id": str(video_id),
            "title": item.get("text") or "",
            "description": item.get("text") or "",
            "uploader": author.get("nickName") or uploader_id,
            "uploader_id": uploader_id,
            "uploader_url": f"https://www.tiktok.com/@{uploader_id}" if uploader_id else None,
            "thumbnail": covers[0] if covers else (carousel_items[0]["url"] if carousel_items else None),
            "duration": item.get("video", {}).get("videoMeta", {}).get("duration"),
            "timestamp": item.get("createTime"),
            "view_count": item.get("playCount"),
            "like_count": item.get("diggCount"),
            "comment_count": item.get("commentCount"),
            "repost_count": item.get("shareCount"),
            "track": music.get("musicName"),
            "artist": music.get("authorName"),
            "webpage_url": resolved_url,
            "original_url": url,
            "direct_video_url": video_url,
            "direct_audio_url": audio_url,
            "is_carousel": is_carousel,
            "carousel_items": carousel_items,
            "media_type": "carousel" if is_carousel else "video",
            "source": "embed_fallback",
        }

        if download:
            outtmpl = extra.get("outtmpl")
            postprocessors = extra.get("postprocessors", [])
            needs_wav = any(p.get("preferredcodec") == "wav" for p in postprocessors)
            dl_url = (video_url or audio_url) if not is_carousel else audio_url
            if outtmpl and dl_url:
                resolved = outtmpl.replace("%(id)s", info["id"])
                ext = "mp4" if (dl_url == video_url and not is_carousel) else "mp3"
                target_file = Path(resolved.replace("%(ext)s", ext))
                r = client.get(dl_url, headers={"Referer": "https://www.tiktok.com/"})
                r.raise_for_status()
                target_file.write_bytes(r.content)
                if needs_wav:
                    wav_file = Path(resolved.replace("%(ext)s", "wav"))
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(target_file),
                            "-vn",
                            "-ar",
                            "16000",
                            "-ac",
                            "1",
                            str(wav_file),
                        ],
                        check=False,
                        capture_output=True,
                    )

        return info


def _normalize_ydl_info(info: dict[str, Any], url: str) -> dict[str, Any]:
    """Normalize yt-dlp info dict, preserving carousel entries if present."""
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise ValueError("No media found at the provided URL")
        if len(entries) > 1 or "/photo/" in url:
            carousel_items = []
            for idx, entry in enumerate(entries, start=1):
                c_ext = entry.get("ext") or ("mp4" if entry.get("vcodec") != "none" else "jpg")
                c_type = "video" if c_ext == "mp4" or entry.get("vcodec") not in (None, "none") else "image"
                carousel_items.append(
                    {
                        "index": idx,
                        "id": entry.get("id"),
                        "type": c_type,
                        "url": entry.get("url"),
                        "thumbnail": entry.get("thumbnail") or entry.get("url"),
                    }
                )
            first = entries[0]
            info = {
                **first,
                "id": info.get("id") or first.get("id"),
                "title": info.get("title") or first.get("title"),
                "description": info.get("description") or first.get("description"),
                "is_carousel": True,
                "carousel_items": carousel_items,
                "media_type": "carousel",
            }
        else:
            info = entries[0]

    if "is_carousel" not in info:
        info["is_carousel"] = False
        info["carousel_items"] = []
        info["media_type"] = "video"

    return info


def _extract(url: str, *, download: bool = False, **extra: Any) -> dict[str, Any]:
    if not download:
        cached = _get_meta_cache(url)
        if cached is not None:
            return cached

    # Photo Mode URLs (/photo/) are best handled via tikwm or embed fallback directly
    if "/photo/" in url:
        for fn in (_extract_fallback, _extract_embed_fallback):
            try:
                info = fn(url, download=download, **extra)
                if not download:
                    _store_meta_cache(url, info)
                return info
            except Exception:
                continue

    try:
        with YoutubeDL(_ydl_opts(**extra)) as ydl:
            raw_info = ydl.extract_info(url, download=download)
        info = _normalize_ydl_info(raw_info or {}, url)
        # If yt-dlp returned a 0-second stub for a photo slideshow, fall back to tikwm/embed
        if not info.get("is_carousel") and info.get("duration") in (0, None) and not download:
            try:
                fb = _extract_fallback(url, download=False)
                if fb.get("is_carousel") or fb.get("duration"):
                    info = fb
            except Exception:
                pass
        if not download:
            _store_meta_cache(url, info)
        return info
    except Exception as exc:
        for fn in (_extract_fallback, _extract_embed_fallback):
            try:
                info = fn(url, download=download, **extra)
                if not download:
                    _store_meta_cache(url, info)
                return info
            except Exception:
                continue
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
    meta: dict[str, Any] = {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "media_type": info.get("media_type") or ("carousel" if info.get("is_carousel") else "video"),
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
        "is_carousel": bool(info.get("is_carousel")),
        "source": info.get("source", "yt-dlp"),
    }
    if info.get("carousel_items"):
        meta["carousel_items"] = info["carousel_items"]
    return meta


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
    if not audio_path.exists() or audio_path.stat().st_size < 1024:
        return {
            "text": "",
            "detected_language": None,
            "language_probability": 0.0,
            "audio_duration_sec": 0.0,
            "model": model_size or CFG.whisper_model,
            "_all_segments": [],
            **({"segments": []} if with_timestamps else {}),
        }

    model = _get_model(model_size)
    kwargs: dict[str, Any] = {
        "beam_size": 5,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.6,
    }
    if languages and len(languages) == 1:
        kwargs["language"] = languages[0]
    elif languages and len(languages) > 1:
        # Single multilingual model handles all selected languages without switching
        kwargs["multilingual"] = True
    kwargs = _filter_kwargs(model.transcribe, kwargs)

    segments, info = model.transcribe(str(audio_path), **kwargs)
    parts: list[str] = []
    timed: list[dict[str, Any]] = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        if getattr(seg, "no_speech_prob", 0.0) > 0.75:
            continue
        if _HALLUCINATION_RE.match(text):
            continue
        parts.append(text)
        timed.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})

    result: dict[str, Any] = {
        "text": " ".join(parts),
        "detected_language": getattr(info, "language", None),
        "language_probability": round(getattr(info, "language_probability", 0.0) or 0.0, 3),
        "audio_duration_sec": round(getattr(info, "duration", 0.0) or 0.0, 2),
        "model": model_size or CFG.whisper_model,
        "_all_segments": timed,
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
    """Returns title, description, cover thumbnail, author, statistics, and carousel slides for a TikTok post.

    Args:
        url: TikTok video or photo-mode URL (full or shortened vm./vt. link).
        fast: When True, uses lightweight oEmbed (instant response, omitting extended statistics).
    """
    return await _video_info(url, fast)


def _extract_wav_from_media(media_path: Path, wav_path: Path) -> bool:
    """Extract 16kHz mono WAV from a media file via ffmpeg. Returns True if successful."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(media_path),
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(wav_path),
            ],
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0 and wav_path.exists() and wav_path.stat().st_size >= 1024
    except Exception:
        return False


async def _transcribe(
    url: str,
    languages: list[str] | None = None,
    with_timestamps: bool = False,
    model_size: str | None = None,
    keep_audio: bool = False,
    include_ocr: bool = True,
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

    video_meta = {
        "id": probe.get("id"),
        "title": probe.get("title"),
        "author": probe.get("uploader_id"),
        "url": probe.get("webpage_url") or url,
    }

    # 1. TikTok Photo Mode / Carousel slideshow: run OCR across all slide images
    if probe.get("is_carousel") and probe.get("carousel_items") and include_ocr and is_ocr_available():
        slide_urls = [
            item.get("url") or item.get("thumbnail")
            for item in probe["carousel_items"]
            if item.get("url") or item.get("thumbnail")
        ]
        if slide_urls:
            ocr_slides = await extract_images_ocr(slide_urls, languages=languages, proxy=CFG.proxy)
            if ocr_slides.get("ocr_available"):
                res: dict[str, Any] = {
                    "text": ocr_slides["ocr_text"],
                    "audio_text": "",
                    "ocr_text": ocr_slides["ocr_text"],
                    "source": "ocr_slides",
                    "detected_language": languages[0] if languages else "auto",
                    "language_probability": 1.0,
                    "audio_duration_sec": float(duration or 0.0),
                    "model": "tesseract-ocr",
                    "ocr_segments": ocr_slides["ocr_segments"],
                    "video": video_meta,
                }
                if with_timestamps:
                    res["segments"] = ocr_slides["ocr_segments"]
                return res

    # 2. Video post: download video (when OCR is enabled) or audio, run Whisper + Keyframe OCR concurrently
    run_visual_ocr = bool(include_ocr and is_ocr_available())

    async with _TRANSCRIBE_SEM:
        tmp_dir = Path(tempfile.mkdtemp(dir=CFG.work_dir, prefix="audio-"))
        try:
            if run_visual_ocr:
                info = await asyncio.to_thread(
                    _extract,
                    url,
                    download=True,
                    format="best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                    merge_output_format="mp4",
                    outtmpl=str(tmp_dir / "%(id)s.%(ext)s"),
                )
            else:
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

            video_file = next(
                (p for p in tmp_dir.iterdir() if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}),
                None,
            )
            audio = next((p for p in tmp_dir.glob("*.wav")), None)

            if audio is None and video_file is not None:
                wav_candidate = tmp_dir / f"{video_file.stem}.wav"
                has_audio = await asyncio.to_thread(_extract_wav_from_media, video_file, wav_candidate)
                if has_audio:
                    audio = wav_candidate

            if audio is None:
                audio = next((p for p in tmp_dir.iterdir() if p.is_file()), tmp_dir / "empty.wav")

            whisper_task = asyncio.to_thread(
                _run_whisper, audio, languages, model_size, with_timestamps
            )
            if run_visual_ocr and video_file is not None:
                ocr_task = extract_video_ocr(video_file, languages=languages)
                result, ocr_res = await asyncio.gather(whisper_task, ocr_task)
            else:
                result = await whisper_task
                ocr_res = {"ocr_available": False, "ocr_text": "", "ocr_segments": []}

            all_audio_segs = result.pop("_all_segments", [])
            audio_text = result.get("text", "")
            combined_text, source_label = merge_speech_and_ocr(audio_text, all_audio_segs, ocr_res)

            # Fallback to cover thumbnail OCR if both speech and video frames yielded no text
            if not combined_text and run_visual_ocr:
                thumb_url = _best_thumbnail(info) or _best_thumbnail(probe)
                if thumb_url:
                    ocr_cover = await extract_images_ocr([thumb_url], languages=languages, proxy=CFG.proxy)
                    if ocr_cover.get("ocr_available"):
                        ocr_res = ocr_cover
                        combined_text = ocr_cover["ocr_text"]
                        source_label = "ocr_cover"

            result["text"] = combined_text
            result["audio_text"] = audio_text
            result["ocr_text"] = ocr_res.get("ocr_text", "")
            result["source"] = source_label
            if ocr_res.get("ocr_segments"):
                result["ocr_segments"] = ocr_res["ocr_segments"]
                if with_timestamps and not result.get("segments"):
                    result["segments"] = ocr_res["ocr_segments"]

            result["video"] = {
                "id": info.get("id") or video_meta["id"],
                "title": info.get("title") or video_meta["title"],
                "author": info.get("uploader_id") or video_meta["author"],
                "url": info.get("webpage_url") or video_meta["url"],
            }
            if keep_audio and audio.exists():
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
    include_ocr: bool = True,
) -> dict[str, Any]:
    """Transcribes a TikTok video or photo slideshow using local Whisper speech-to-text and visual OCR.

    Automatically extracts both spoken audio (via faster-whisper) and on-screen text from
    videos with screenshots/slides or TikTok Photo Mode carousels (via ffmpeg + Tesseract OCR).

    Args:
        url: TikTok video or photo URL.
        languages: 1-3 language codes (e.g., ['ru', 'en']). One locks the model;
            multiple uses a single multilingual model; None enables auto-detection.
        with_timestamps: Return segment breakdown with start and end timestamps.
        model_size: Whisper model override (tiny/base/small/medium/large-v3/large-v3-turbo).
        keep_audio: Retain downloaded audio file on disk instead of removing it.
        include_ocr: Extract on-screen text from video keyframes and photo slides via OCR (default: True).
    """
    return await _transcribe(url, languages, with_timestamps, model_size, keep_audio, include_ocr)


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
        include_transcript: Transcribe speech and on-screen text and attach to video metadata.
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
            meta["audio_transcript"] = transcript.get("audio_text", "")
            meta["ocr_transcript"] = transcript.get("ocr_text", "")
            meta["transcript_source"] = transcript.get("source", "whisper")
            meta["transcript_language"] = transcript["detected_language"]

        meta["video_url"] = await _upload(target, "video/mp4", meta)

        if meta.get("thumbnail"):
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(meta["thumbnail"])
            if r.status_code == 200:
                ext = ".webp" if "webp" in r.headers.get("content-type", "") else ".jpg"
                cover = tmp_dir / f"{name}-cover{ext}"
                cover.write_bytes(r.content)
                meta["cover_url"] = await _upload(
                    cover, r.headers.get("content-type", "image/jpeg"), meta
                )

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
    """stdio transport when an MCP client launches the process; http/sse when running as a daemon."""
    transport = os.getenv("TIKTOK_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    elif transport == "sse":
        mcp.run(
            transport="sse",
            host=os.getenv("TIKTOK_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("TIKTOK_MCP_PORT", "8770")),
        )
    else:
        mcp.run(
            transport="http",
            host=os.getenv("TIKTOK_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("TIKTOK_MCP_PORT", "8770")),
        )


if __name__ == "__main__":
    main()
