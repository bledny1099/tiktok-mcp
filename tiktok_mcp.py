"""TikTok MCP server.

Метаданные и медиа берутся скрейпингом публичной страницы через yt-dlp
(без TikTok Developer API / OAuth). Расшифровка — локально, faster-whisper.
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
        "Работа с публичными видео TikTok без официального API: метаданные "
        "(название, описание, превью, автор, статистика), расшифровка речи "
        "в текст и публикация видео на собственный домен."
    ),
)

# --------------------------------------------------------------------------- #
# Конфигурация (env)
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    work_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("TIKTOK_MCP_WORK_DIR", tempfile.gettempdir()) or tempfile.gettempdir()
        )
        / "tiktok-mcp"
    )
    # cookies: путь к cookies.txt ИЛИ имя браузера (chrome/firefox/edge/safari)
    cookies_file: str | None = os.getenv("TIKTOK_MCP_COOKIES_FILE") or None
    cookies_browser: str | None = os.getenv("TIKTOK_MCP_COOKIES_BROWSER") or None
    proxy: str | None = os.getenv("TIKTOK_MCP_PROXY") or None

    # whisper
    whisper_model: str = os.getenv("TIKTOK_MCP_WHISPER_MODEL", "large-v3-turbo")
    whisper_device: str = os.getenv("TIKTOK_MCP_WHISPER_DEVICE", "auto")
    whisper_compute: str = os.getenv("TIKTOK_MCP_WHISPER_COMPUTE", "int8")

    # публикация
    upload_mode: Literal["http", "local", "off"] = os.getenv("TIKTOK_MCP_UPLOAD_MODE", "off")  # type: ignore[assignment]
    upload_url: str | None = os.getenv("TIKTOK_MCP_UPLOAD_URL") or None
    upload_token: str | None = os.getenv("TIKTOK_MCP_UPLOAD_TOKEN") or None
    local_dir: str | None = os.getenv("TIKTOK_MCP_LOCAL_DIR") or None
    public_base_url: str = os.getenv("TIKTOK_MCP_PUBLIC_BASE_URL", "https://dosimple.app/media")


CFG = Config()
CFG.work_dir.mkdir(parents=True, exist_ok=True)

_URL_RE = re.compile(r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/\S+", re.I)
_HASHTAG_RE = re.compile(r"#([\w\u0400-\u04FF]+)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _validate_url(url: str) -> str:
    url = url.strip()
    if not _URL_RE.match(url):
        raise ValueError(f"Не похоже на ссылку TikTok: {url!r}")
    return url


def _slugify(text: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")[:60]
    return slug or fallback


# --------------------------------------------------------------------------- #
# yt-dlp
# --------------------------------------------------------------------------- #


def _ydl_opts(**extra: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 3,
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
    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
    with httpx.Client(proxy=CFG.proxy, timeout=20, follow_redirects=True) as client:
        resp = client.get(api_url)
        data = resp.json()
        if data.get("code") != 0 or not data.get("data"):
            raise ValueError(f"Fallback API error: {data.get('msg')}")
        d = data["data"]
        info = {
            "id": str(d.get("id")),
            "title": d.get("title"),
            "description": d.get("title"),
            "uploader": d.get("author", {}).get("nickname"),
            "uploader_id": d.get("author", {}).get("unique_id"),
            "uploader_url": f"https://www.tiktok.com/@{d.get('author', {}).get('unique_id')}",
            "thumbnail": d.get("cover"),
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
            video_url = d.get("play")
            audio_url = d.get("music") or video_url

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
                raise ValueError("По ссылке не найдено ни одного видео")
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
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "title": info.get("title") or description[:100] or None,
        "description": description,
        "hashtags": _HASHTAG_RE.findall(description),
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
        "source": "yt-dlp",
    }


async def _oembed(url: str) -> dict[str, Any]:
    """Лёгкий публичный oEmbed-эндпоинт: только title/author/thumbnail, без ключей."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get("https://www.tiktok.com/oembed", params={"url": url})
        r.raise_for_status()
        data = r.json()
    return {
        "id": data.get("embed_product_id"),
        "url": url,
        "title": data.get("title"),
        "description": data.get("title"),
        "hashtags": _HASHTAG_RE.findall(data.get("title") or ""),
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
    """Отбрасывает параметры, которых нет в текущей версии faster-whisper."""
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
        # одна мультиязычная модель на все выбранные языки, без переключения моделей
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
# Загрузка на свой домен
# --------------------------------------------------------------------------- #


async def _upload(path: Path, content_type: str, meta: dict[str, Any]) -> str:
    if CFG.upload_mode == "off":
        raise RuntimeError(
            "Публикация выключена. Задайте TIKTOK_MCP_UPLOAD_MODE=http|local и связанные переменные."
        )
    if CFG.upload_mode == "local":
        if not CFG.local_dir:
            raise RuntimeError("TIKTOK_MCP_LOCAL_DIR не задан")
        dest_dir = Path(CFG.local_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest_dir / path.name)
        return f"{CFG.public_base_url.rstrip('/')}/{path.name}"

    if not CFG.upload_url:
        raise RuntimeError("TIKTOK_MCP_UPLOAD_URL не задан")
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
# Инструменты MCP
# --------------------------------------------------------------------------- #


@mcp.tool
async def get_video_info(url: str, fast: bool = False) -> dict[str, Any]:
    """Название, описание, превью, автор и статистика ролика TikTok.

    Args:
        url: ссылка на видео (полная или короткая vm./vt.).
        fast: True — только oEmbed (мгновенно, но без описания и статистики).
    """
    url = _validate_url(url)
    if fast:
        return await _oembed(url)
    try:
        info = await asyncio.to_thread(_extract, url)
        return _pack_meta(info)
    except DownloadError as exc:
        # частая причина — TikTok требует свежие cookies; отдаём хоть что-то
        fallback = await _oembed(url)
        fallback["warning"] = f"yt-dlp не смог разобрать страницу ({exc}); отдан oEmbed-минимум"
        return fallback


@mcp.tool
async def transcribe_video(
    url: str,
    languages: list[str] | None = None,
    with_timestamps: bool = False,
    model_size: str | None = None,
    keep_audio: bool = False,
) -> dict[str, Any]:
    """Скачивает дорожку ролика и переводит речь в текст локально.

    Args:
        url: ссылка на видео TikTok.
        languages: 1-3 кода языка ("ru", "en"). Один — жёсткая фиксация;
            несколько — одна мультиязычная модель на все сразу; None — автоопределение.
        with_timestamps: вернуть посегментную разбивку с таймкодами.
        model_size: переопределить модель whisper (tiny/base/small/medium/large-v3/large-v3-turbo).
        keep_audio: не удалять скачанный аудиофайл.
    """
    url = _validate_url(url)
    if languages and len(languages) > 3:
        raise ValueError("Максимум 3 языка за раз")

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
async def publish_video(
    url: str,
    slug: str | None = None,
    include_transcript: bool = True,
    languages: list[str] | None = None,
    max_height: int = 1080,
) -> dict[str, Any]:
    """Скачивает видео + превью и заливает их на свой домен, возвращая публичные ссылки.

    Args:
        url: ссылка на видео TikTok.
        slug: имя файла без расширения (по умолчанию — из названия ролика).
        include_transcript: приложить расшифровку речи к метаданным.
        languages: языки для расшифровки (см. transcribe_video).
        max_height: ограничение по высоте видео.
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
            transcript = await transcribe_video.fn(url=url, languages=languages)  # type: ignore[attr-defined]
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
    """Метаданные для нескольких роликов сразу (до 20 ссылок)."""
    if len(urls) > 20:
        raise ValueError("Максимум 20 ссылок за вызов")
    results = await asyncio.gather(
        *(get_video_info.fn(url=u, fast=fast) for u in urls),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    return [
        {"url": u, "error": str(r)} if isinstance(r, Exception) else r
        for u, r in zip(urls, results)
    ]


def main() -> None:
    """stdio — если MCP-клиент сам запускает процесс; http — если сервер живёт демоном."""
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
