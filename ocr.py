"""Visual OCR extraction for TikTok videos with screenshots/slides and photo carousels.

Extracts on-screen text from:
1. Video files: scene-change + interval keyframe extraction via ffmpeg,
   center-crop perceptual deduplication (ignoring corner watermarks), dark-mode
   inversion + contrast preprocessing via Pillow, and Tesseract TSV OCR with
   confidence filtering and cross-frame fuzzy line deduplication.
2. Photo Mode carousels / slide images: concurrent image download and per-slide OCR.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("tiktok_mcp.ocr")

_OCR_SEM = asyncio.Semaphore(int(os.getenv("TIKTOK_MCP_OCR_CONCURRENCY", "4")))
_TESSERACT_BIN: str | None = shutil.which("tesseract")
_FFMPEG_BIN: str | None = shutil.which("ffmpeg")
_AVAILABLE_LANGS: set[str] | None = None

# Common platform watermark patterns to strip from frame edges
_WATERMARK_RE = re.compile(
    r"^(?:tiktok|reels|instagram|youtube\s*shorts|capcut|inshot|vn)\b",
    re.IGNORECASE,
)


def is_ocr_available() -> bool:
    """Return True if tesseract binary is installed on the system."""
    global _TESSERACT_BIN
    if _TESSERACT_BIN is None:
        _TESSERACT_BIN = shutil.which("tesseract")
    return bool(_TESSERACT_BIN)


def _resolve_tesseract_langs(preferred: list[str] | str | None = None) -> str:
    """Resolve Tesseract language string (default 'rus+eng') against installed packs."""
    global _AVAILABLE_LANGS, _TESSERACT_BIN
    if not _TESSERACT_BIN:
        _TESSERACT_BIN = shutil.which("tesseract")
    if not _TESSERACT_BIN:
        return "eng"

    if _AVAILABLE_LANGS is None:
        try:
            proc = subprocess.run(
                [_TESSERACT_BIN, "--list-langs"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = [
                ln.strip()
                for ln in (proc.stdout + "\n" + proc.stderr).splitlines()
                if ln.strip()
            ]
            _AVAILABLE_LANGS = {ln for ln in lines if not ln.lower().startswith("list of")}
        except Exception:
            _AVAILABLE_LANGS = {"eng", "rus"}

    if isinstance(preferred, list):
        target = "+".join(preferred)
    else:
        target = preferred or os.getenv("TIKTOK_MCP_OCR_LANGUAGES", "rus+eng")

    lang_map = {
        "ru": "rus",
        "ru-ru": "rus",
        "en": "eng",
        "eng-us": "eng",
        "en-us": "eng",
        "uk": "ukr",
        "de": "deu",
        "fr": "fra",
        "es": "spa",
    }
    requested: list[str] = []
    for part in re.split(r"[+,]", target):
        clean = part.strip().lower()
        if not clean or clean == "auto":
            continue
        mapped = lang_map.get(clean, clean)
        if mapped not in requested:
            requested.append(mapped)

    # Always include rus and eng if available so bilingual screenshots work seamlessly
    for fallback in ("rus", "eng"):
        if fallback not in requested:
            requested.append(fallback)

    valid = [lang for lang in requested if lang in _AVAILABLE_LANGS]
    if not valid:
        return "eng"
    return "+".join(valid)


def _format_timestamp(seconds: float) -> str:
    """Format seconds into MM:SS.mmm or HH:MM:SS.mmm."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    ms = int((seconds - int(seconds)) * 1000)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}.{ms:03d}"


def _compute_center_dhash(img_path: str) -> tuple[int, float] | None:
    """Compute a 256-bit difference hash and mean brightness on the central 80% of an image."""
    try:
        from PIL import Image, ImageStat

        with Image.open(img_path) as im:
            gray = im.convert("L")
            w, h = gray.size
            if w > 40 and h > 40:
                crop_box = (int(w * 0.1), int(h * 0.1), int(w * 0.9), int(h * 0.9))
                center = gray.crop(crop_box)
            else:
                center = gray

            mean_lum = ImageStat.Stat(center).mean[0]
            resized = center.resize((17, 16), Image.Resampling.BILINEAR)
            pixels = list(resized.getdata())
            bits = 0
            for row in range(16):
                row_offset = row * 17
                for col in range(16):
                    bits = (bits << 1) | (
                        1 if pixels[row_offset + col] > pixels[row_offset + col + 1] else 0
                    )
            return bits, mean_lum
    except Exception:
        return None


def _hamming_distance(h1: int, h2: int) -> int:
    return (h1 ^ h2).bit_count()


def _prepare_ocr_variants(img_path: str, tmp_dir: str) -> list[str]:
    """Create grayscale (and inverted dark-mode if dark) variants for OCR without clipping text pixels."""
    try:
        from PIL import Image, ImageOps, ImageStat

        with Image.open(img_path) as im:
            gray = im.convert("L")
            w, h = gray.size
            if w < 1000:
                scale = min(2.0, 1200.0 / max(1, w))
                gray = gray.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

            mean_lum = ImageStat.Stat(gray).mean[0]
            base_name = Path(img_path).stem

            # Full-range autocontrast with cutoff=0 so sparse text pixels (<1% area) are never clipped
            norm = ImageOps.autocontrast(gray, cutoff=0)
            norm_path = os.path.join(tmp_dir, f"{base_name}_norm.png")
            norm.save(norm_path)

            # Dark-mode or mixed-contrast screenshot: invert so white text becomes black on white
            if mean_lum < 140:
                inv = ImageOps.autocontrast(ImageOps.invert(gray), cutoff=0)
                inv_path = os.path.join(tmp_dir, f"{base_name}_inv.png")
                inv.save(inv_path)
                return [inv_path, norm_path] if mean_lum < 110 else [norm_path, inv_path]

            return [norm_path]
    except Exception:
        return [img_path]


def _is_valid_token(word: str, conf: float) -> bool:
    """Filter out OCR noise tokens while preserving real Russian/English words and numbers."""
    w = word.strip()
    if not w:
        return False
    if conf < 42:
        return False

    alnum = re.findall(r"[A-Za-zА-Яа-яЁё0-9]", w)
    if not alnum:
        return False

    if len(w) == 1:
        if w.lower() in {"в", "и", "с", "к", "о", "у", "я", "а", "i", "a"} or w.isdigit():
            return conf >= 55
        return conf >= 80

    if len(w) == 2 and conf < 55:
        return False

    if len(alnum) == 1 and len(w) >= 3:
        return False

    return True


def _run_tesseract_tsv_sync(img_path: str, lang_str: str) -> tuple[str, float, int]:
    """Run Tesseract in TSV mode and return (cleaned_text, weighted_score, word_count)."""
    if not _TESSERACT_BIN:
        return "", 0.0, 0

    cmd = [
        _TESSERACT_BIN,
        img_path,
        "stdout",
        "-l",
        lang_str,
        "--oem",
        "1",
        "--psm",
        "6",
        "tsv",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0 or not proc.stdout:
            return "", 0.0, 0
    except Exception:
        return "", 0.0, 0

    lines_map: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []

    for row in proc.stdout.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12:
            continue
        try:
            level = int(parts[0])
            block_num = int(parts[2])
            par_num = int(parts[3])
            line_num = int(parts[4])
            conf = float(parts[10])
            text = parts[11].strip()
        except ValueError:
            continue

        if level != 5 or not text:
            continue
        if not _is_valid_token(text, conf):
            continue

        key = (block_num, par_num, line_num)
        lines_map.setdefault(key, []).append(text)
        confs.append(conf)

    if not confs:
        return "", 0.0, 0

    cleaned_lines: list[str] = []
    total_words = 0
    for words in lines_map.values():
        line_str = " ".join(words).strip()
        if not line_str:
            continue
        if _WATERMARK_RE.match(line_str) and len(words) <= 2:
            continue
        if len(words) == 1 and len(line_str) <= 2 and not line_str.isdigit():
            continue
        cleaned_lines.append(line_str)
        total_words += len(words)

    if not cleaned_lines:
        return "", 0.0, 0

    avg_conf = sum(confs) / len(confs)
    if avg_conf < 52 or (total_words < 2 and len(cleaned_lines[0]) < 5):
        return "", 0.0, 0

    final_text = "\n".join(cleaned_lines)
    score = avg_conf * (total_words**0.6)
    return final_text, score, total_words


def _ocr_single_image_sync(img_path: str, tmp_dir: str, lang_str: str) -> str:
    """Run OCR on an image, testing normal and dark-mode inverted variants when appropriate."""
    variants = _prepare_ocr_variants(img_path, tmp_dir)
    best_text = ""
    best_score = 0.0

    for var_path in variants:
        text, score, words = _run_tesseract_tsv_sync(var_path, lang_str)
        if score > best_score:
            best_score = score
            best_text = text
        if words >= 6 and score >= 180.0:
            break

    return best_text


def normalize_for_comparison(text: str) -> str:
    """Normalize text for cross-frame or speech-vs-OCR comparison."""
    return re.sub(r"[^\wа-яё]+", " ", text.lower()).strip()


def _merge_or_deduplicate_frames(
    raw_frames: list[tuple[float, str]]
) -> list[dict[str, Any]]:
    """Deduplicate consecutive video frames that show the same or scrolling screenshots."""
    if not raw_frames:
        return []

    segments: list[dict[str, Any]] = []
    seen_lines_norm: list[str] = []

    for idx, (ts, text) in enumerate(raw_frames):
        next_ts = raw_frames[idx + 1][0] if idx + 1 < len(raw_frames) else round(ts + 2.5, 2)
        norm_curr = normalize_for_comparison(text)
        if not norm_curr:
            continue

        if segments:
            prev_seg = segments[-1]
            norm_prev = normalize_for_comparison(prev_seg["text"])
            similarity = difflib.SequenceMatcher(None, norm_prev, norm_curr).ratio()

            if similarity >= 0.80 or norm_curr in norm_prev:
                prev_seg["end"] = round(next_ts, 2)
                prev_seg["end_formatted"] = _format_timestamp(next_ts)
                if len(norm_curr) > len(norm_prev) * 1.15 and norm_prev in norm_curr:
                    prev_seg["text"] = text
                continue

        new_lines: list[str] = []
        for line in text.splitlines():
            ln_norm = normalize_for_comparison(line)
            if not ln_norm:
                continue
            is_dup = any(
                difflib.SequenceMatcher(None, ln_norm, prev_ln).ratio() >= 0.86
                for prev_ln in seen_lines_norm[-25:]
            )
            if not is_dup:
                new_lines.append(line)
                seen_lines_norm.append(ln_norm)

        if not new_lines:
            if segments:
                segments[-1]["end"] = round(next_ts, 2)
                segments[-1]["end_formatted"] = _format_timestamp(next_ts)
            continue

        clean_block = "\n".join(new_lines)
        segments.append(
            {
                "start": round(ts, 2),
                "end": round(next_ts, 2),
                "start_formatted": _format_timestamp(ts),
                "end_formatted": _format_timestamp(next_ts),
                "text": clean_block,
            }
        )

    return segments


def _extract_video_ocr_sync(
    video_path: str | Path,
    languages: list[str] | str | None = None,
    max_frames: int = 18,
) -> dict[str, Any]:
    """Extract scene/interval keyframes from a local video file and run OCR."""
    video_str = str(video_path)
    if not is_ocr_available() or not _FFMPEG_BIN or not os.path.exists(video_str):
        return {"ocr_available": False, "ocr_text": "", "ocr_segments": [], "frames_analyzed": 0}

    lang_str = _resolve_tesseract_langs(languages)

    with tempfile.TemporaryDirectory(prefix="tiktok_ocr_") as tmp_dir:
        out_pattern = os.path.join(tmp_dir, "frame_%03d.jpg")
        vf_expr = (
            "select='eq(n\\,0)+gt(scene\\,0.10)+isnan(prev_selected_t)+gte(t-prev_selected_t\\,2.0)',"
            "scale='min(1280\\,iw)':-2,"
            "showinfo"
        )
        cmd = [
            _FFMPEG_BIN,
            "-hide_banner",
            "-i",
            video_str,
            "-vf",
            vf_expr,
            "-vsync",
            "vfr",
            "-frames:v",
            str(max_frames),
            "-q:v",
            "2",
            out_pattern,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception as e:
            logger.debug("ffmpeg frame extraction failed: %s", e)
            return {"ocr_available": False, "ocr_text": "", "ocr_segments": [], "frames_analyzed": 0}

        timestamps = [float(m) for m in re.findall(r"pts_time:\s*([\d.]+)", proc.stderr or "")]
        frame_files = sorted(Path(tmp_dir).glob("frame_*.jpg"))
        if not frame_files:
            return {"ocr_available": False, "ocr_text": "", "ocr_segments": [], "frames_analyzed": 0}

        unique_frames: list[tuple[float, str]] = []
        last_hash: int | None = None

        for idx, fpath in enumerate(frame_files):
            ts = timestamps[idx] if idx < len(timestamps) else round(idx * 2.0, 2)
            h_res = _compute_center_dhash(str(fpath))
            if h_res is not None:
                curr_hash, _ = h_res
                if last_hash is not None and _hamming_distance(curr_hash, last_hash) <= 5:
                    continue
                last_hash = curr_hash
            unique_frames.append((ts, str(fpath)))

        ocr_raw: list[tuple[float, str]] = []
        for ts, fpath in unique_frames:
            text = _ocr_single_image_sync(fpath, tmp_dir, lang_str)
            if text:
                ocr_raw.append((ts, text))

        segments = _merge_or_deduplicate_frames(ocr_raw)
        ocr_text = "\n\n".join(s["text"] for s in segments)
        return {
            "ocr_available": bool(segments),
            "ocr_text": ocr_text,
            "ocr_segments": segments,
            "frames_analyzed": len(unique_frames),
        }


async def extract_video_ocr(
    video_path: str | Path,
    languages: list[str] | str | None = None,
    max_frames: int = 18,
) -> dict[str, Any]:
    """Asynchronously extract on-screen text from a video file via keyframe OCR."""
    if not is_ocr_available():
        return {"ocr_available": False, "ocr_text": "", "ocr_segments": [], "frames_analyzed": 0}
    async with _OCR_SEM:
        return await asyncio.to_thread(_extract_video_ocr_sync, video_path, languages, max_frames)


async def extract_images_ocr(
    image_urls: list[str],
    languages: list[str] | str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Download photo-mode / carousel images concurrently and extract text from each slide via OCR."""
    if not is_ocr_available() or not image_urls:
        return {"ocr_available": False, "ocr_text": "", "ocr_segments": [], "slides_analyzed": 0}

    lang_str = _resolve_tesseract_langs(languages)
    urls = [u for u in image_urls if u][:25]

    client_kwargs: dict[str, Any] = {
        "timeout": 20.0,
        "follow_redirects": True,
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
    }
    if proxy:
        client_kwargs["proxy"] = proxy

    with tempfile.TemporaryDirectory(prefix="tiktok_slides_") as tmp_dir:
        downloaded: list[tuple[int, str]] = []

        async with httpx.AsyncClient(**client_kwargs) as client:
            async def _fetch_slide(idx: int, url: str) -> tuple[int, str] | None:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200 and resp.content:
                        dest = os.path.join(tmp_dir, f"slide_{idx:02d}.jpg")
                        with open(dest, "wb") as f:
                            f.write(resp.content)
                        return idx, dest
                except Exception as e:
                    logger.debug("Failed to download slide %d (%s): %s", idx, url, e)
                return None

            results = await asyncio.gather(*(_fetch_slide(i, u) for i, u in enumerate(urls, start=1)))
            downloaded = [r for r in results if r is not None]

        if not downloaded:
            return {"ocr_available": False, "ocr_text": "", "ocr_segments": [], "slides_analyzed": 0}

        async with _OCR_SEM:
            def _run_slides() -> list[dict[str, Any]]:
                segs: list[dict[str, Any]] = []
                for idx, path in sorted(downloaded, key=lambda x: x[0]):
                    txt = _ocr_single_image_sync(path, tmp_dir, lang_str)
                    if txt:
                        segs.append(
                            {
                                "slide": idx,
                                "start": float(idx - 1),
                                "end": float(idx),
                                "text": txt,
                            }
                        )
                return segs

            segments = await asyncio.to_thread(_run_slides)

        ocr_text = "\n\n".join(f"[Slide {s['slide']}]\n{s['text']}" for s in segments)
        return {
            "ocr_available": bool(segments),
            "ocr_text": ocr_text,
            "ocr_segments": segments,
            "slides_analyzed": len(downloaded),
        }


def merge_speech_and_ocr(
    audio_text: str,
    audio_segments: list[dict[str, Any]],
    ocr_res: dict[str, Any],
) -> tuple[str, str]:
    """Combine spoken audio transcript with visual OCR text, avoiding burned-in subtitle duplication.

    Returns (combined_text, source_label).
    """
    audio_clean = (audio_text or "").strip()
    ocr_clean = (ocr_res.get("ocr_text") or "").strip()

    if not ocr_clean:
        return audio_clean, "whisper" if audio_clean else "none"

    if not audio_clean:
        return ocr_clean, "ocr_video"

    norm_audio = normalize_for_comparison(audio_clean)
    norm_ocr = normalize_for_comparison(ocr_clean)

    if norm_ocr and norm_audio:
        if norm_ocr in norm_audio or difflib.SequenceMatcher(None, norm_audio, norm_ocr).ratio() >= 0.72:
            return audio_clean, "whisper"

        # Filter out individual OCR segments that merely repeat spoken lines
        unique_ocr_blocks: list[str] = []
        for seg in ocr_res.get("ocr_segments") or []:
            seg_txt = (seg.get("text") or "").strip()
            seg_norm = normalize_for_comparison(seg_txt)
            if not seg_norm:
                continue
            if seg_norm in norm_audio:
                continue
            is_sub = any(
                difflib.SequenceMatcher(
                    None, seg_norm, normalize_for_comparison(a_seg.get("text", ""))
                ).ratio()
                >= 0.75
                for a_seg in audio_segments
            )
            if not is_sub:
                unique_ocr_blocks.append(seg_txt)

        if not unique_ocr_blocks:
            return audio_clean, "whisper"

        filtered_ocr = "\n\n".join(unique_ocr_blocks)
        combined = f"{audio_clean}\n\n[On-screen text (OCR)]\n{filtered_ocr}"
        return combined, "whisper+ocr"

    return f"{audio_clean}\n\n[On-screen text (OCR)]\n{ocr_clean}", "whisper+ocr"
