from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
try:
    from app.database.db import (
        mark_task_done,
        get_pending_tasks,
        get_tasks_with_details,
        add_task,
        update_task,
        delete_task,
        add_material,
        add_materials_batch,
        delete_material,
        get_materials_for_task,
        get_materials_for_tasks_batch,
        get_materials_and_comments_batch,
        get_material_by_id,
        upsert_user,
        get_user,
        get_all_users,
        is_using_supabase,
        get_notification_prefs,
        upsert_notification_prefs,
        get_leaderboard,
        cleanup_old_tasks,
        add_task_comment,
        get_task_comments,
        get_comments_count_batch,
        delete_task_comment,
        load_analytics_snapshot,
        save_analytics_snapshot,
        clear_analytics_snapshot,
        get_historical_activity
    )
    from app.database.admin_config import is_admin_identity, resolve_effective_role
except ImportError:
    from db import (
        mark_task_done,
        get_pending_tasks,
        get_tasks_with_details,
        add_task,
        update_task,
        delete_task,
        add_material,
        add_materials_batch,
        delete_material,
        get_materials_for_task,
        get_materials_for_tasks_batch,
        get_materials_and_comments_batch,
        get_material_by_id,
        upsert_user,
        get_user,
        get_all_users,
        is_using_supabase,
        get_notification_prefs,
        upsert_notification_prefs,
        get_leaderboard,
        cleanup_old_tasks,
        add_task_comment,
        get_task_comments,
        get_comments_count_batch,
        delete_task_comment,
        load_analytics_snapshot,
        save_analytics_snapshot,
        clear_analytics_snapshot,
        get_historical_activity
    )
    from admin_config import is_admin_identity, resolve_effective_role
import uuid
import os
import json
import gzip
import io
import time
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
import mimetypes
import base64
import hashlib
import asyncio
import logging
from urllib.parse import quote
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("server")

app = FastAPI()
# Сжимает HTML/JSON ответы в ~5-6 раз (изображения/видео уже исключены по умолчанию)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

@app.middleware("http")
async def add_static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") and response.status_code == 200 and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response

_root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_uploads_dir = os.path.join(_root_dir, "uploads")
_static_dir = os.path.join(_root_dir, "static")
_index_file = os.path.join(_static_dir, "index.html")

os.makedirs(_uploads_dir, exist_ok=True)
if os.path.exists(_uploads_dir):
    app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")
if os.path.exists(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Кэш index.html (raw + pre-gzipped) в RAM с обновлением по mtime для 0ms отдачи
_INDEX_CACHE: Dict[str, Any] = {"mtime_ns": -1, "bytes": b"", "gz_bytes": b"", "etag": ""}

def _get_cached_index_payload() -> tuple[bytes, bytes, str]:
    path = _index_file if os.path.exists(_index_file) else os.path.join(_root_dir, "static", "index.html")
    st = os.stat(path)
    if _INDEX_CACHE["mtime_ns"] != st.st_mtime_ns or not _INDEX_CACHE["bytes"]:
        with open(path, "rb") as f:
            raw = f.read()
        gz_raw = gzip.compress(raw, compresslevel=6)
        etag = f'"{hashlib.md5(raw).hexdigest()}"'
        _INDEX_CACHE["mtime_ns"] = st.st_mtime_ns
        _INDEX_CACHE["bytes"] = raw
        _INDEX_CACHE["gz_bytes"] = gz_raw
        _INDEX_CACHE["etag"] = etag
    return _INDEX_CACHE["bytes"], _INDEX_CACHE["gz_bytes"], _INDEX_CACHE["etag"]

async def _prewarm_backend_caches():
    """Прогревает TLS-пул Supabase, ленту заданий, рейтинг и index.html при старте сервера."""
    try:
        await asyncio.gather(
            asyncio.to_thread(get_tasks_with_details, 0),
            asyncio.to_thread(get_leaderboard, "10-А"),
            asyncio.to_thread(_get_cached_index_payload),
        )
        logger.info("⚡ [Startup] Кэши заданий, рейтинга и index.html успешно прогреты")
    except Exception as e:
        logger.warning(f"Startup prewarm warning: {e}")

_ANALYTICS_LOCK = threading.Lock()
_ANALYTICS_STATE: Dict[str, Any] = {
    "users": {},
    "actions": {},
    "events": deque(maxlen=2000),
    "pending_events": [],
    "dirty": False,
}

def _hydrate_analytics():
    users_map, actions_map, log_events = load_analytics_snapshot(event_limit=1500)
    with _ANALYTICS_LOCK:
        _ANALYTICS_STATE["users"].update(users_map)
        _ANALYTICS_STATE["actions"].update(actions_map)
        _ANALYTICS_STATE["events"].extend(log_events)

def _flush_analytics_sync():
    with _ANALYTICS_LOCK:
        if not _ANALYTICS_STATE["dirty"] and not _ANALYTICS_STATE["pending_events"]:
            return
        users_snapshot = [dict(v) for v in _ANALYTICS_STATE["users"].values()]
        actions_snapshot = [dict(v) for v in _ANALYTICS_STATE["actions"].values()]
        pending_snapshot = list(_ANALYTICS_STATE["pending_events"])
        all_events_snapshot = list(_ANALYTICS_STATE["events"])
        _ANALYTICS_STATE["pending_events"].clear()
        _ANALYTICS_STATE["dirty"] = False
    save_analytics_snapshot(users_snapshot, actions_snapshot, pending_snapshot, all_events=all_events_snapshot)

async def run_periodic_analytics_flush():
    """Периодически сбрасывает накопленную в RAM аналитику в WAL SQLite (0мс для пользователей)."""
    while True:
        await asyncio.sleep(15)
        try:
            await asyncio.to_thread(_flush_analytics_sync)
        except Exception as e:
            logger.warning(f"Analytics flush error: {e}")

# ─────────────────────────────────────────────────────────────
# ФОНОВАЯ АВТОМАТИЧЕСКАЯ ОЧИСТКА ЗАДАНИЙ СТАРШЕ 30 ДНЕЙ
# ─────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup_tasks():
    await asyncio.to_thread(_hydrate_analytics)
    asyncio.create_task(_prewarm_backend_caches())
    asyncio.create_task(run_periodic_cleanup_loop())
    asyncio.create_task(run_periodic_analytics_flush())

@app.on_event("shutdown")
async def on_shutdown_tasks():
    await asyncio.to_thread(_flush_analytics_sync)

async def run_periodic_cleanup_loop():
    """Фоновый цикл автоочистки устаревших заданий (> 30 дней) каждые 12 часов."""
    while True:
        try:
            cleaned = await asyncio.to_thread(cleanup_old_tasks, 30)
            if cleaned > 0:
                logger.info(f"🧹 [Auto-Cleanup] Успешно удалено {cleaned} устаревших заданий старше 30 дней")
        except Exception as e:
            logger.warning(f"Auto-cleanup error: {e}")
        await asyncio.sleep(43200)  # Раз в 12 часов

# Сверхбыстрый многоуровневый кэш фото и миниатюр (RAM -> Диск uploads/ -> Supabase)
# Устраняет повторные сетевые запросы к БД и отдает материалы за 0-2 миллисекунды
_MATERIAL_CACHE: Dict[int, Dict[str, Any]] = {}
_MATERIAL_THUMB_CACHE: Dict[tuple, Dict[str, Any]] = {}

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_DOCUMENTS_PER_UPLOAD = 5

_BLOCKED_FILE_EXTS = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".sh", ".vbs", ".vbe",
    ".jse", ".wsf", ".wsh", ".ps1", ".scr", ".dll", ".pif", ".apk",
    ".jar", ".cpl", ".hta", ".reg",
}

_INLINE_SAFE_MIMES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
    "application/pdf",
    "text/plain", "text/csv", "text/markdown",
}

_EXT_TO_MIME: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".json": "application/json",
    ".xml": "application/xml",
    ".sql": "application/sql",
    ".cpp": "text/x-c++src",
    ".c": "text/x-csrc",
    ".h": "text/x-chdr",
    ".java": "text/x-java-source",
    ".ipynb": "application/json",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
}

_MIME_TO_EXT: Dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/rtf": ".rtf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/vnd.rar": ".rar",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/json": ".json",
    "application/xml": ".xml",
}

def _safe_cache_ext(filename: str, fallback: str = ".bin") -> str:
    """Возвращает безопасное ASCII-расширение файла для имени кэша на диске."""
    _, ext = os.path.splitext((filename or "").strip())
    cleaned = "".join(c for c in ext.lower() if c.isalnum() or c == ".")
    if cleaned.startswith(".") and 2 <= len(cleaned) <= 12:
        return cleaned
    return fallback

def guess_file_mime(filename: str, fallback_mime: str = "") -> str:
    """Определяет точный MIME-тип по расширению файла с безопасным fallback."""
    _, ext = os.path.splitext((filename or "").strip())
    ext_lower = ext.lower()
    if ext_lower in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext_lower]
    clean_fallback = (fallback_mime or "").split(";", 1)[0].strip().lower()
    if clean_fallback and clean_fallback != "application/octet-stream":
        return clean_fallback
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"

def _detect_mime_from_bytes(data: bytes) -> str:
    """Определяет MIME-тип изображения по сигнатуре байтов при fallback."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/jpeg"

def build_content_disposition(
    filename: str,
    media_type: str = "image/jpeg",
    material_id: int = 0,
    disposition: str = "inline",
) -> str:
    """
    Формирует RFC 6266/5987 Content-Disposition с безопасным ASCII fallback и UTF-8 filename*.
    Исключает UnicodeEncodeError ('latin-1' codec can't encode...) для кириллических имен файлов.
    """
    base_mime = (media_type or "").split(";", 1)[0].strip().lower()
    default_ext = _MIME_TO_EXT.get(base_mime) or (".jpg" if base_mime.startswith("image/") else (mimetypes.guess_extension(base_mime) or ".bin"))
    raw_clean = (filename or "").replace("\r", " ").replace("\n", " ").replace('"', "'").replace("\\", "/").strip()
    clean = os.path.basename(raw_clean) or f"material_{material_id}{default_ext}"
    stem, ext = os.path.splitext(clean)
    if not ext:
        ext = default_ext
        clean_full = f"{clean}{ext}"
        stem = clean
    else:
        clean_full = clean
    ascii_stem = stem.encode("ascii", "ignore").decode("ascii")
    ascii_stem = "".join(c if (c.isalnum() or c in " -_.") else "_" for c in ascii_stem).strip(" ._-\t")
    if not ascii_stem:
        ascii_stem = f"material_{material_id}"
    ascii_ext = ext.encode("ascii", "ignore").decode("ascii")
    ascii_ext = "".join(c for c in ascii_ext if c.isalnum() or c == ".") or default_ext
    ascii_name = f"{ascii_stem}{ascii_ext}"
    utf8_encoded = quote(clean_full, safe="")
    disp = "attachment" if disposition == "attachment" else "inline"
    return f'{disp}; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_encoded}'

def optimize_image_bytes(image_bytes: bytes, max_dim: int = 1600, quality: int = 80) -> tuple[bytes, str]:
    """
    Интеллектуальная оптимизация и сжатие загружаемых фото:
    - Автоматически выравнивает ориентацию по EXIF смартфонов (устраняет перевернутые фото).
    - Уменьшает гигантские снимки смартфонов (4000px -> до 1600px по длинной стороне).
    - Сохраняет в прогрессивный JPEG с качеством 80% (идеальная четкость формул и рукописного текста при весе ~120-250 КБ).
    - Экономит до 90% диска и трафика Supabase.
    """
    try:
        from PIL import Image, ImageOps, ImageFile
        import io
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            w, h = img.size
            if w > max_dim or h > max_dim:
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"Image optimization note: {e}")
        return image_bytes, _detect_mime_from_bytes(image_bytes)

def _etag_matches(request: Request, etag: str) -> bool:
    inm = request.headers.get("if-none-match")
    return bool(inm and (etag in inm or inm.strip() == "*"))

@app.get("/")
def route(request: Request):
    raw_bytes, gz_bytes, etag = _get_cached_index_payload()
    headers = {
        "ETag": etag,
        "Cache-Control": "no-cache, must-revalidate",
        "Vary": "Accept-Encoding",
    }
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    accept_enc = (request.headers.get("accept-encoding") or "").lower()
    if "gzip" in accept_enc and gz_bytes:
        headers["Content-Encoding"] = "gzip"
        return Response(content=gz_bytes, media_type="text/html; charset=utf-8", headers=headers)
    return Response(content=raw_bytes, media_type="text/html; charset=utf-8", headers=headers)

def _serve_static_file(rel_path: str, media_type: str, headers: Optional[Dict[str, str]] = None):
    path = os.path.join(_static_dir, rel_path)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(path, media_type=media_type, headers=headers or {})

@app.get("/manifest.webmanifest")
@app.get("/manifest.json")
def pwa_manifest():
    return _serve_static_file(
        "manifest.webmanifest",
        media_type="application/manifest+json; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )

@app.get("/sw.js")
def pwa_service_worker():
    return _serve_static_file(
        "sw.js",
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/api/app-version")
def get_app_version():
    """Возвращает текущую версию сборки (ETag index.html + mtime sw.js) для авто-обновления PWA."""
    _, _, etag = _get_cached_index_payload()
    sw_path = os.path.join(_static_dir, "sw.js")
    sw_mtime = int(os.stat(sw_path).st_mtime) if os.path.isfile(sw_path) else 0
    return Response(
        content=json.dumps({"version": f"{etag.strip(chr(34))}:{sw_mtime}"}),
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )

@app.get("/favicon.ico")
def pwa_favicon():
    return _serve_static_file(
        os.path.join("pwa", "favicon-32.png"),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )

@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def pwa_apple_touch_icon():
    return _serve_static_file(
        os.path.join("pwa", "apple-touch-icon.png"),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )

@app.get("/test-backpack")
def test_backpack():
    backpack_path = "scratch/test_backpack_real.html"
    if os.path.exists(backpack_path):
        return FileResponse(backpack_path)
    return {"status": "ok", "message": "Backpack preview not active"}

@app.get("/api/status")
@app.get("/health")
@app.get("/ping")
def get_db_status():
    return {
        "status": "ok",
        "backend": "supabase" if is_using_supabase() else "sqlite"
    }

@app.get("/api/tasks")
def get_tasks(request: Request, user_id: int):
    results = get_tasks_with_details(user_id)
    body = json.dumps(results, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    etag = f'"{hashlib.md5(body).hexdigest()}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "no-cache, must-revalidate",
    }
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json; charset=utf-8", headers=headers)

def _save_material_cache(
    material_id: int,
    content: bytes,
    media_type: str,
    filename: str,
    is_doc_file: bool = False,
) -> Dict[str, Any]:
    """Сохраняет материал в RAM и на диск (для документов — с метаданными имени и MIME)."""
    etag = f'"{hashlib.md5(content).hexdigest()}"'
    cached = {
        "content": content,
        "media_type": media_type,
        "etag": etag,
        "filename": filename,
    }
    _MATERIAL_CACHE[material_id] = cached
    try:
        os.makedirs(_uploads_dir, exist_ok=True)
        if is_doc_file:
            safe_ext = _safe_cache_ext(filename, ".bin")
            cache_file = f"cached_file_{material_id}{safe_ext}"
            with open(os.path.join(_uploads_dir, cache_file), "wb") as f:
                f.write(content)
            meta_path = os.path.join(_uploads_dir, f"cached_file_{material_id}.meta.json")
            with open(meta_path, "w", encoding="utf-8") as mf:
                json.dump(
                    {"filename": filename, "media_type": media_type, "cache_file": cache_file},
                    mf,
                    ensure_ascii=False,
                )
        else:
            ext = _MIME_TO_EXT.get((media_type or "").lower(), ".jpg")
            if ext not in (".jpg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            with open(os.path.join(_uploads_dir, f"cached_{material_id}{ext}"), "wb") as f:
                f.write(content)
    except Exception:
        pass
    return cached


def _get_or_build_thumbnail(material_id: int, cached: Dict[str, Any], prefer_webp: bool) -> Optional[Dict[str, Any]]:
    """Генерирует и кэширует лёгкую миниатюру (до 360px) для мгновенной загрузки ленты."""
    out_fmt = "WEBP" if prefer_webp else "JPEG"
    ext = ".webp" if prefer_webp else ".jpg"
    mime = "image/webp" if prefer_webp else "image/jpeg"
    cache_key = (material_id, out_fmt)

    mem_thumb = _MATERIAL_THUMB_CACHE.get(cache_key)
    if mem_thumb:
        return mem_thumb

    disk_thumb = os.path.join(_uploads_dir, f"thumb_{material_id}{ext}")
    if os.path.isfile(disk_thumb):
        try:
            with open(disk_thumb, "rb") as tf:
                tbytes = tf.read()
            if tbytes:
                entry = {
                    "content": tbytes,
                    "media_type": mime,
                    "etag": f'"{hashlib.md5(tbytes).hexdigest()}-{out_fmt.lower()}"',
                }
                if len(_MATERIAL_THUMB_CACHE) > 400:
                    _MATERIAL_THUMB_CACHE.clear()
                _MATERIAL_THUMB_CACHE[cache_key] = entry
                return entry
        except Exception:
            pass

    try:
        from PIL import Image, ImageOps, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(io.BytesIO(cached["content"])) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((360, 360), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            if out_fmt == "WEBP":
                img.save(buf, format="WEBP", quality=76, method=4)
            else:
                img.save(buf, format="JPEG", quality=76, optimize=True)
            tbytes = buf.getvalue()
        try:
            os.makedirs(_uploads_dir, exist_ok=True)
            with open(disk_thumb, "wb") as tf:
                tf.write(tbytes)
        except Exception:
            pass
        entry = {
            "content": tbytes,
            "media_type": mime,
            "etag": f'"{hashlib.md5(tbytes).hexdigest()}-{out_fmt.lower()}"',
        }
        if len(_MATERIAL_THUMB_CACHE) > 400:
            _MATERIAL_THUMB_CACHE.clear()
        _MATERIAL_THUMB_CACHE[cache_key] = entry
        return entry
    except Exception:
        return None


def _resolve_material_payload(material_id: int) -> Dict[str, Any]:
    """Извлекает материал из RAM, локального кэша uploads/ или БД (без дублирования логики)."""
    cached = _MATERIAL_CACHE.get(material_id)

    if not cached:
        meta_path = os.path.join(_uploads_dir, f"cached_file_{material_id}.meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as mf:
                    meta = json.load(mf)
                cache_name = os.path.basename(meta.get("cache_file") or "")
                disk_path = os.path.join(_uploads_dir, cache_name) if cache_name else ""
                if disk_path and os.path.isfile(disk_path):
                    with open(disk_path, "rb") as f:
                        data = f.read()
                    cached = {
                        "content": data,
                        "media_type": meta.get("media_type") or "application/octet-stream",
                        "etag": f'"{hashlib.md5(data).hexdigest()}"',
                        "filename": meta.get("filename") or cache_name,
                    }
                    _MATERIAL_CACHE[material_id] = cached
            except Exception:
                pass

    if not cached:
        for ext, mime in [(".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp"), (".gif", "image/gif")]:
            disk_path = os.path.join(_uploads_dir, f"cached_{material_id}{ext}")
            if os.path.exists(disk_path):
                try:
                    with open(disk_path, "rb") as f:
                        data = f.read()
                    cached = {
                        "content": data,
                        "media_type": mime,
                        "etag": f'"{hashlib.md5(data).hexdigest()}"',
                        "filename": f"material_{material_id}{ext}",
                    }
                    _MATERIAL_CACHE[material_id] = cached
                    break
                except Exception:
                    pass

    if not cached:
        mat = get_material_by_id(material_id)
        if not mat:
            raise HTTPException(status_code=404, detail="Material not found")

        cap = mat.get("caption") or ""
        fid = mat.get("file_id") or ""
        if cap.startswith("[LINK]"):
            raw = cap[6:]
            link_url = raw.split("::", 1)[0].strip() if "::" in raw else raw.strip()
            return {"redirect_url": link_url}
        elif fid.startswith(("http://", "https://")):
            return {"redirect_url": fid}

        is_doc_file = cap.startswith("[FILE]")
        if is_doc_file:
            raw_file_meta = cap[6:]
            fname_part = raw_file_meta.rsplit("::", 1)[0] if "::" in raw_file_meta else raw_file_meta
            filename = os.path.basename(fname_part.strip()) or f"file_{material_id}.bin"
            media_type = guess_file_mime(filename)
        else:
            caption = cap or f"material_{material_id}.jpg"
            filename = caption if caption.lower().endswith((".jpg", ".png", ".jpeg", ".webp", ".gif")) else f"{caption}.jpg"
            media_type = "image/jpeg"

        content = None
        if fid.startswith("data:"):
            try:
                header, b64_data = fid.split(",", 1)
                parsed_mime = header.split(";")[0].replace("data:", "").strip()
                media_type = guess_file_mime(filename, parsed_mime) if is_doc_file else (parsed_mime or "image/jpeg")
                content = base64.b64decode(b64_data)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to decode file data: {e}")
        else:
            clean_path = fid.lstrip("/")
            candidate_path = os.path.join(_root_dir, clean_path) if not os.path.isabs(clean_path) else clean_path
            base_upload_dir = os.path.abspath(_uploads_dir)
            abs_clean_path = os.path.abspath(candidate_path)
            if not (abs_clean_path == base_upload_dir or abs_clean_path.startswith(base_upload_dir + os.sep)):
                raise HTTPException(status_code=403, detail="Forbidden access")
            if os.path.exists(abs_clean_path):
                try:
                    with open(abs_clean_path, "rb") as f:
                        content = f.read()
                    media_type = guess_file_mime(filename if is_doc_file else abs_clean_path)
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")

        if not content:
            raise HTTPException(status_code=404, detail="File content not found")

        cached = _save_material_cache(material_id, content, media_type, filename, is_doc_file=is_doc_file)

    return {"cached": cached}


@app.get("/api/materials/{material_id}/download")
@app.get("/api/materials/{material_id}")
def download_material(material_id: int, request: Request):
    resolved = _resolve_material_payload(material_id)
    if "redirect_url" in resolved:
        return RedirectResponse(url=resolved["redirect_url"], status_code=302)
    cached = resolved["cached"]

    base_mime = (cached.get("media_type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    want_thumb = request.query_params.get("thumb") == "1" and not request.url.path.endswith("/download")
    if want_thumb and base_mime.startswith("image/") and base_mime != "image/gif":
        prefer_webp = "image/webp" in (request.headers.get("accept") or "").lower()
        thumb_entry = _get_or_build_thumbnail(material_id, cached, prefer_webp)
        if thumb_entry:
            client_etag = request.headers.get("if-none-match")
            if client_etag and client_etag == thumb_entry["etag"]:
                return Response(
                    status_code=304,
                    headers={
                        "ETag": thumb_entry["etag"],
                        "Cache-Control": "public, max-age=2592000, immutable",
                        "Vary": "Accept",
                    },
                )
            return Response(
                content=thumb_entry["content"],
                media_type=thumb_entry["media_type"],
                headers={
                    "Cache-Control": "public, max-age=2592000, immutable",
                    "ETag": thumb_entry["etag"],
                    "Vary": "Accept",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    # Проверяем HTTP ETag (браузерный кэш 304 Not Modified без повторного скачивания)
    client_etag = request.headers.get("if-none-match")
    if client_etag and client_etag == cached["etag"]:
        return Response(
            status_code=304,
            headers={
                "ETag": cached["etag"],
                "Cache-Control": "public, max-age=2592000, immutable"
            }
        )

    # Отдаем материал с правильным Content-Disposition и долгосрочным кэшем (30 дней)
    force_download = request.url.path.endswith("/download") or request.query_params.get("download") == "1"
    disp_mode = "attachment" if (force_download or base_mime not in _INLINE_SAFE_MIMES) else "inline"
    content_disp = build_content_disposition(
        cached.get("filename", ""),
        media_type=base_mime,
        material_id=material_id,
        disposition=disp_mode,
    )
    return Response(
        content=cached["content"],
        media_type=cached["media_type"],
        headers={
            "Content-Disposition": content_disp,
            "Cache-Control": "public, max-age=2592000, immutable",
            "ETag": cached["etag"],
            "X-Content-Type-Options": "nosniff",
        }
    )


class SendToChatRequest(BaseModel):
    user_id: int


async def _send_telegram_document(chat_id: int, filename: str, content: bytes, mime_type: str, caption: str) -> Dict[str, Any]:
    """Отправляет документ пользователю в личный чат через Telegram Bot API."""
    if chat_id <= 0 or chat_id == 12345:
        return {"sent": False, "reason": "guest"}
    bot_token = (os.getenv("BOT_TOKEN") or "").strip()
    if not bot_token:
        return {"sent": False, "reason": "no_bot_token"}
    api_base = f"https://api.telegram.org/bot{bot_token}"
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(
            f"{api_base}/sendDocument",
            data={"chat_id": str(chat_id), "caption": caption[:1024]},
            files={"document": (filename, content, mime_type)},
        )
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code == 200 and data.get("ok"):
        return {"sent": True, "kind": "document", "filename": filename}
    desc = str(data.get("description") or "").lower()
    if resp.status_code == 403 or "chat not found" in desc or "blocked" in desc:
        return {"sent": False, "reason": "start_bot"}
    return {"sent": False, "reason": "telegram_error", "detail": data.get("description") or f"HTTP {resp.status_code}"}


@app.post("/api/materials/{material_id}/send-to-chat")
async def send_material_to_chat(material_id: int, req: SendToChatRequest):
    """Отправляет файл или фото напрямую в личный чат пользователя в Telegram через бота."""
    if req.user_id <= 0 or req.user_id == 12345:
        return {"sent": False, "reason": "guest"}

    bot_token = (os.getenv("BOT_TOKEN") or "").strip()
    if not bot_token:
        return {"sent": False, "reason": "no_bot_token"}

    resolved = await asyncio.to_thread(_resolve_material_payload, material_id)
    api_base = f"https://api.telegram.org/bot{bot_token}"

    async with httpx.AsyncClient(timeout=25.0) as client:
        if "redirect_url" in resolved:
            resp = await client.post(
                f"{api_base}/sendMessage",
                json={"chat_id": req.user_id, "text": f"🔗 {resolved['redirect_url']}"},
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 200 and data.get("ok"):
                return {"sent": True, "kind": "link", "filename": resolved["redirect_url"]}
            return {"sent": False, "reason": "start_bot" if resp.status_code in (400, 403) else "telegram_error"}

        cached = resolved["cached"]
        content = cached["content"]
        base_mime = (cached.get("media_type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        filename = cached.get("filename") or f"material_{material_id}"
        is_photo = base_mime in ("image/jpeg", "image/jpg", "image/png", "image/webp")

        if is_photo:
            resp = await client.post(
                f"{api_base}/sendPhoto",
                data={"chat_id": str(req.user_id), "caption": f"🖼 {filename}"[:1024]},
                files={"photo": (filename, content, base_mime)},
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 200 and data.get("ok"):
                return {"sent": True, "kind": "photo", "filename": filename}
            desc = str(data.get("description") or "").lower()
            if resp.status_code == 403 or "chat not found" in desc or "blocked" in desc:
                return {"sent": False, "reason": "start_bot"}

        # Документы или фолбэк (если Telegram отклонил размер/пропорции как фото)
        resp = await client.post(
            f"{api_base}/sendDocument",
            data={"chat_id": str(req.user_id), "caption": f"📎 {filename}"[:1024]},
            files={"document": (filename, content, base_mime)},
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 200 and data.get("ok"):
            return {"sent": True, "kind": "document", "filename": filename}
        desc = str(data.get("description") or "").lower()
        if resp.status_code == 403 or "chat not found" in desc or "blocked" in desc:
            return {"sent": False, "reason": "start_bot"}
        return {"sent": False, "reason": "telegram_error", "detail": data.get("description") or f"HTTP {resp.status_code}"}

class TaskDone(BaseModel):
    user_id: int
    task_id: int

@app.post('/api/done')
def mark_done(data: TaskDone):
    mark_task_done(data.task_id, data.user_id)
    return {'status': 'ok'}

class TaskCreate(BaseModel):
    user_id: int
    subject: str = Field(..., max_length=50)
    description: str = Field(..., max_length=1500)
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    telegram_nick: Optional[str] = None
    full_name: Optional[str] = None

@app.post('/api/tasks')
def create_task(data: TaskCreate):
    task_id = add_task(
        user_id=data.user_id,
        subject=data.subject,
        description=data.description,
        due_date=data.due_date,
        telegram_nick=data.telegram_nick,
        full_name=data.full_name
    )
    return {"status": "ok", "task_id": task_id}

class TaskUpdate(BaseModel):
    user_id: int
    subject: str = Field(..., max_length=50)
    description: str = Field(..., max_length=1500)
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    editor_name: Optional[str] = None

@app.put('/api/tasks/{task_id}')
@app.post('/api/tasks/{task_id}/edit')
def edit_task(task_id: int, data: TaskUpdate):
    success, msg = update_task(task_id, data.subject, data.description, data.due_date, data.user_id, data.editor_name)
    if not success:
        raise HTTPException(status_code=403, detail=msg)
    return {"status": "ok", "task_id": task_id, "message": msg}

class TaskDelete(BaseModel):
    user_id: int

@app.delete('/api/tasks/{task_id}')
@app.post('/api/tasks/{task_id}/delete')
def remove_task(task_id: int, user_id: Optional[int] = None, data: Optional[TaskDelete] = None):
    effective_user_id = user_id or (data.user_id if data else None)
    success, msg = delete_task(task_id, effective_user_id)
    if not success:
        raise HTTPException(status_code=403, detail=msg)
    return {"status": "ok", "message": msg}

@app.delete('/api/materials/{material_id}')
@app.post('/api/materials/{material_id}/delete')
def remove_material(material_id: int):
    # Очищаем кэш в RAM и на диске (включая кэши документов .meta.json и миниатюры)
    _MATERIAL_CACHE.pop(material_id, None)
    _MATERIAL_THUMB_CACHE.pop((material_id, "WEBP"), None)
    _MATERIAL_THUMB_CACHE.pop((material_id, "JPEG"), None)
    prefixes = (f"cached_{material_id}.", f"cached_file_{material_id}.", f"cached_doc_{material_id}.", f"thumb_{material_id}.")
    try:
        for fname in os.listdir(_uploads_dir):
            if fname.startswith(prefixes):
                try:
                    os.remove(os.path.join(_uploads_dir, fname))
                except Exception:
                    pass
    except Exception:
        pass
    delete_material(material_id)
    return {"status": "ok"}

class LinkMaterialRequest(BaseModel):
    task_id: int
    user_id: int
    url: str
    title: Optional[str] = ""

class BatchLinksRequest(BaseModel):
    task_id: int
    user_id: int
    links: List[Dict[str, str]]

@app.post("/api/materials/link")
def add_link_material(req: LinkMaterialRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    title = (req.title or "").strip()
    caption = f"[LINK]{url}::{title}"
    created = add_materials_batch(req.task_id, [{"file_id": url, "caption": caption}], req.user_id)
    return {"status": "ok", "created": created}

@app.post("/api/materials/links")
def add_batch_links(req: BatchLinksRequest):
    items = []
    for l in req.links:
        u = (l.get("url") or "").strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        t = (l.get("title") or "").strip()
        items.append({
            "file_id": u,
            "caption": f"[LINK]{u}::{t}"
        })
    if items:
        created = add_materials_batch(req.task_id, items, req.user_id)
        return {"status": "ok", "created": created}
    return {"status": "ok", "created": []}

# --- Комментарии к домашним заданиям ---
class CommentCreate(BaseModel):
    user_id: int
    text: str
    author_name: Optional[str] = None
    author_nick: Optional[str] = None

@app.get("/api/tasks/{task_id}/comments")
def api_get_task_comments(task_id: int):
    comments = get_task_comments(task_id)
    return {"status": "ok", "comments": comments}

@app.post("/api/tasks/{task_id}/comments")
def api_add_task_comment(task_id: int, data: CommentCreate):
    txt = (data.text or "").strip()
    if not txt:
        raise HTTPException(status_code=400, detail="Comment text cannot be empty")
    if len(txt) > 2000:
        raise HTTPException(status_code=400, detail="Comment is too long (max 2000 chars)")

    created = add_task_comment(
        task_id=task_id,
        user_id=data.user_id,
        text=txt,
        author_name=data.author_name,
        author_nick=data.author_nick
    )
    if not created:
        raise HTTPException(status_code=500, detail="Failed to save comment")
    return {"status": "ok", "comment": created}

@app.delete("/api/comments/{comment_id}")
@app.post("/api/comments/{comment_id}/delete")
def api_delete_task_comment(comment_id: int, user_id: int = 0):
    success = delete_task_comment(comment_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=403, detail="Cannot delete comment")
    return {"status": "deleted", "id": comment_id}

def _is_admin_user(user_id: int = 0, admin_secret: Optional[str] = None) -> bool:
    secret_required = (os.getenv("ADMIN_SECRET_KEY") or "").strip()
    if secret_required and admin_secret and admin_secret == secret_required:
        return True
    if is_admin_identity(user_id=user_id):
        return True
    if user_id and int(user_id) > 0:
        u = get_user(int(user_id))
        if isinstance(u, dict) and is_admin_identity(
            user_id=user_id,
            telegram_nick=u.get("telegram_nick"),
            role=u.get("role"),
        ):
            return True
    return False


class UserSync(BaseModel):
    user_id: int
    telegram_nick: Optional[str] = None
    full_name: Optional[str] = None


@app.post('/api/user/sync')
def sync_user(data: UserSync):
    clean_nick = (data.telegram_nick or "").strip() or None
    if clean_nick and clean_nick.lower() in ("@ученик_10а", "@ник"):
        clean_nick = None
    clean_name = (data.full_name or "").strip() or None
    if clean_name == "Ученик":
        clean_name = None

    existing = get_user(data.user_id)
    existing_role = existing.get("role") if isinstance(existing, dict) else None
    existing_nick = existing.get("telegram_nick") if isinstance(existing, dict) else None
    effective_role = resolve_effective_role(
        user_id=data.user_id,
        telegram_nick=clean_nick or existing_nick,
        existing_role=existing_role,
    )
    if existing and (
        (not clean_nick or existing.get("telegram_nick") == clean_nick)
        and (not clean_name or existing.get("full_name") == clean_name)
        and existing_role == effective_role
    ):
        return {"status": "ok", "role": effective_role}

    upsert_user(
        data.user_id,
        telegram_nick=clean_nick,
        full_name=clean_name,
        role=effective_role,
    )
    return {"status": "ok", "role": effective_role}

@app.post("/api/upload")
async def upload_photos(
    task_id: int = Form(...),
    user_id: int = Form(...),
    photos: List[UploadFile] = File(None),
    photo: Optional[UploadFile] = File(None)
):
    try:
        os.makedirs(_uploads_dir, exist_ok=True)
        all_incoming: List[UploadFile] = []
        if photos:
            all_incoming.extend(photos)
        if photo:
            all_incoming.append(photo)

        uploaded_results = []
        batch_items = []
        items_contents = []

        for p in all_incoming:
            raw_name = (p.filename or "photo.jpg").strip() or "photo.jpg"
            raw_contents = await p.read()
            if not raw_contents:
                continue

            # Оптимизация и сжатие фото
            contents, mime = optimize_image_bytes(raw_contents, max_dim=1600, quality=80)
            ext = _MIME_TO_EXT.get(mime.lower(), ".jpg")
            unique_name = f"{task_id}_{uuid.uuid4().hex[:8]}{ext}"
            file_path = f"uploads/{unique_name}"
            disk_file_path = os.path.join(_uploads_dir, unique_name)

            # 1. Сохраняем на локальный диск
            try:
                with open(disk_file_path, "wb") as f:
                    f.write(contents)
            except Exception as disk_err:
                print(f"Disk write warning: {disk_err}")

            b64_str = base64.b64encode(contents).decode("utf-8")
            data_url = f"data:{mime};base64,{b64_str}"

            storage_ref = data_url if is_using_supabase() else file_path

            batch_items.append({"file_id": storage_ref, "caption": raw_name})
            uploaded_results.append({"url": file_path, "name": raw_name})
            items_contents.append((contents, mime, raw_name))

        if batch_items:
            created_ids = add_materials_batch(task_id, batch_items, user_id)
            if created_ids is False:
                raise HTTPException(status_code=500, detail="Failed to save uploaded photos to database")
            # Моментально прогреваем кэш в RAM и на диске для новых материалов
            if isinstance(created_ids, list):
                for idx, mat_id in enumerate(created_ids):
                    if idx < len(items_contents):
                        c_bytes, c_mime, c_name = items_contents[idx]
                        _save_material_cache(mat_id, c_bytes, c_mime, c_name, is_doc_file=False)

        # Возвращаем url первого файла для обратной совместимости + список всех
        first_url = uploaded_results[0]["url"] if uploaded_results else ""
        return {"status": "ok", "url": first_url, "uploaded": uploaded_results}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in /api/upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/materials/files")
async def upload_files(
    task_id: int = Form(...),
    user_id: int = Form(...),
    files: List[UploadFile] = File(None),
    file: Optional[UploadFile] = File(None),
):
    """Загружает документы, презентации, архивы и файлы кода к домашнему заданию."""
    try:
        os.makedirs(_uploads_dir, exist_ok=True)
        all_incoming: List[UploadFile] = []
        if files:
            all_incoming.extend(files)
        if file:
            all_incoming.append(file)

        if not all_incoming:
            raise HTTPException(status_code=400, detail="Файлы не выбраны")
        if len(all_incoming) > MAX_DOCUMENTS_PER_UPLOAD:
            raise HTTPException(status_code=400, detail=f"Максимум {MAX_DOCUMENTS_PER_UPLOAD} файлов за одну загрузку")

        uploaded_results = []
        batch_items = []
        items_contents = []

        for f in all_incoming:
            raw_base = os.path.basename((f.filename or "").replace("\\", "/").replace("\r", " ").replace("\n", " ").replace("::", " ").strip())
            raw_name = raw_base or f"document_{uuid.uuid4().hex[:6]}.bin"
            _, ext = os.path.splitext(raw_name)
            ext_lower = ext.lower()
            if ext_lower in _BLOCKED_FILE_EXTS:
                raise HTTPException(status_code=400, detail=f"Формат {ext_lower} запрещён по соображениям безопасности")

            raw_contents = await f.read()
            if not raw_contents:
                continue
            if len(raw_contents) > MAX_DOCUMENT_BYTES:
                raise HTTPException(status_code=413, detail=f"Файл «{raw_name}» превышает лимит 10 МБ")

            mime = guess_file_mime(raw_name, f.content_type or "")
            safe_ext = _safe_cache_ext(raw_name, _MIME_TO_EXT.get(mime, ".bin"))
            unique_name = f"{task_id}_{uuid.uuid4().hex[:8]}{safe_ext}"
            file_path = f"uploads/{unique_name}"
            disk_file_path = os.path.join(_uploads_dir, unique_name)

            try:
                with open(disk_file_path, "wb") as out_f:
                    out_f.write(raw_contents)
            except Exception as disk_err:
                print(f"Document disk write warning: {disk_err}")

            b64_str = base64.b64encode(raw_contents).decode("utf-8")
            data_url = f"data:{mime};base64,{b64_str}"
            storage_ref = data_url if is_using_supabase() else file_path
            caption = f"[FILE]{raw_name}::{len(raw_contents)}"

            batch_items.append({"file_id": storage_ref, "caption": caption})
            uploaded_results.append({
                "name": raw_name,
                "size": len(raw_contents),
                "mime": mime,
                "url": file_path,
            })
            items_contents.append((raw_contents, mime, raw_name))

        if not batch_items:
            raise HTTPException(status_code=400, detail="Выбранные файлы пусты")

        created_ids = add_materials_batch(task_id, batch_items, user_id)
        if created_ids is False:
            raise HTTPException(status_code=500, detail="Failed to save uploaded files to database")

        if isinstance(created_ids, list):
            for idx, mat_id in enumerate(created_ids):
                if idx < len(items_contents):
                    c_bytes, c_mime, c_name = items_contents[idx]
                    _save_material_cache(mat_id, c_bytes, c_mime, c_name, is_doc_file=True)
                if idx < len(uploaded_results):
                    uploaded_results[idx]["id"] = mat_id
                    uploaded_results[idx]["url"] = f"/api/materials/{mat_id}"
                    uploaded_results[idx]["download_url"] = f"/api/materials/{mat_id}/download"

        return {"status": "ok", "uploaded": uploaded_results}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in /api/materials/files: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class NotifPrefsRequest(BaseModel):
    user_id: int
    notify_08: bool = False
    notify_16: bool = False
    notify_18: bool = True
    notify_weekly: bool = False
    notify_today: bool = True
    notify_tomorrow: bool = True
    notify_2days: bool = True
    notify_overdue: bool = True
    notify_custom: bool = False
    custom_time: Optional[str] = ""
    custom_times: Optional[List[str]] = None

@app.get("/api/notifications/prefs")
def get_user_notification_prefs(user_id: int):
    return get_notification_prefs(user_id)

@app.post("/api/notifications/prefs")
def update_user_notification_prefs(payload: NotifPrefsRequest):
    success = upsert_notification_prefs(
        payload.user_id,
        notify_08=payload.notify_08,
        notify_16=payload.notify_16,
        notify_18=payload.notify_18,
        notify_2days=payload.notify_2days,
        notify_weekly=payload.notify_weekly,
        notify_today=payload.notify_today,
        notify_tomorrow=payload.notify_tomorrow,
        notify_overdue=payload.notify_overdue,
        notify_custom=payload.notify_custom,
        custom_time=payload.custom_time or "",
        custom_times=payload.custom_times
    )
    prefs = get_notification_prefs(payload.user_id)
    return {"status": "ok" if success else "error", "prefs": prefs}

@app.get("/api/leaderboard")
def api_get_leaderboard(class_name: str = "10-А", refresh: bool = False):
    return {
        "status": "ok",
        "class_name": class_name,
        "leaderboard": get_leaderboard(class_name, force_refresh=refresh),
        "cache_interval_hours": 4
    }

@app.post("/api/tasks/cleanup")
@app.get("/api/tasks/cleanup")
def api_cleanup_old_tasks(days: int = 30, admin_secret: Optional[str] = None):
    """Ручной и автоматический эндпоинт для очистки заданий старше указанного количества дней."""
    secret_required = os.getenv("ADMIN_SECRET_KEY")
    if secret_required and admin_secret != secret_required:
        raise HTTPException(status_code=403, detail="Admin secret required")
    safe_days = max(14, int(days))
    deleted_count = cleanup_old_tasks(days=safe_days)
    return {
        "status": "ok",
        "deleted_tasks": deleted_count,
        "days_threshold": safe_days,
        "message": f"Очищено {deleted_count} заданий старше {safe_days} дней"
    }

# ─────────────────────────────────────────────────────────────
# ЛЁГКАЯ ТЕЛЕМЕТРИЯ ПОСЕЩЕНИЙ, КЛИКОВ И ОНЛАЙН-СТАТУСА
# ─────────────────────────────────────────────────────────────
def _clean_real_name(val: str) -> str:
    s = (val or "").strip()
    if not s or s == "Ученик" or s.startswith("ID ") or s.startswith("Ученик #"):
        return ""
    return s


def _clean_real_nick(val: str) -> str:
    s = (val or "").strip()
    if not s or s == "@ученик_10а":
        return ""
    return s


_SCHOOL_TZ = timezone(timedelta(hours=5))


def _fmt_local_dt(ts: float) -> str:
    if not ts or ts <= 0:
        return "—"
    return datetime.fromtimestamp(float(ts), tz=_SCHOOL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_analytics_label(val: str, fallback: str) -> str:
    s = (val or "").strip()
    if s in ("—", "✕", "") or s.lower() == "закрыть":
        return fallback
    return s


@app.post("/api/analytics/events")
async def ingest_analytics_events(request: Request):
    """Принимает пакеты событий телеметрии (включая navigator.sendBeacon) за <1мс в RAM."""
    try:
        raw_body = await request.body()
        if not raw_body:
            return {"ok": True, "accepted": 0}
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return {"ok": False, "accepted": 0}

    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not events:
        return {"ok": True, "accepted": 0}

    top_uid = payload.get("user_id")
    top_name = str(payload.get("name") or "").strip()[:120]
    top_nick = str(payload.get("nick") or "").strip()[:80]
    top_platform = str(payload.get("platform") or "Web").strip()[:24]
    now = time.time()
    accepted = 0

    with _ANALYTICS_LOCK:
        users_map = _ANALYTICS_STATE["users"]
        actions_map = _ANALYTICS_STATE["actions"]
        events_log = _ANALYTICS_STATE["events"]
        pending_events = _ANALYTICS_STATE["pending_events"]

        for ev in events[:40]:
            if not isinstance(ev, dict):
                continue
            try:
                uid = int(ev.get("user_id") if ev.get("user_id") is not None else (top_uid or 0))
            except (TypeError, ValueError):
                uid = 0
            if uid <= 0:
                continue

            ev_type = str(ev.get("type") or "click").strip().lower()
            ev_key = str(ev.get("key") or ev_type).strip()[:100]
            ev_label = _sanitize_analytics_label(str(ev.get("label") or ev_key), ev_key)[:120]
            u_name = str(ev.get("name") or top_name).strip()[:120]
            u_nick = str(ev.get("nick") or top_nick).strip()[:80]
            u_plat = str(ev.get("platform") or top_platform or "Web").strip()[:24]

            u_entry = users_map.get(uid)
            if not u_entry:
                u_entry = {
                    "user_id": uid,
                    "name": u_name,
                    "nick": u_nick,
                    "platform": u_plat,
                    "visits": 0,
                    "clicks": 0,
                    "last_seen": now,
                    "first_seen": now,
                    "last_action": "",
                    "explicit_offline": False,
                }
                users_map[uid] = u_entry

            clean_name = _clean_real_name(u_name)
            clean_nick = _clean_real_nick(u_nick)
            if clean_name:
                u_entry["name"] = clean_name
            if clean_nick:
                u_entry["nick"] = clean_nick
            if u_plat:
                u_entry["platform"] = u_plat
            if u_entry["first_seen"] <= 0:
                u_entry["first_seen"] = now
            u_entry["last_seen"] = now

            if ev_type == "leave":
                u_entry["explicit_offline"] = True
                log_item = {
                    "ts": now,
                    "user_id": uid,
                    "type": "leave",
                    "key": ev_key,
                    "label": ev_label or "Закрыл приложение",
                    "platform": u_entry["platform"],
                }
                events_log.append(log_item)
                pending_events.append(log_item)
            elif ev_type == "heartbeat":
                u_entry["explicit_offline"] = False
            elif ev_type == "visit":
                u_entry["explicit_offline"] = False
                u_entry["visits"] += 1
                u_entry["last_action"] = ev_label or "Открыл приложение"
                log_item = {
                    "ts": now,
                    "user_id": uid,
                    "type": "visit",
                    "key": ev_key,
                    "label": u_entry["last_action"],
                    "platform": u_entry["platform"],
                }
                events_log.append(log_item)
                pending_events.append(log_item)
            else:
                u_entry["explicit_offline"] = False
                u_entry["clicks"] += 1
                u_entry["last_action"] = ev_label
                act = actions_map.get(ev_key)
                if not act:
                    actions_map[ev_key] = {"key": ev_key, "label": ev_label, "clicks": 1}
                else:
                    if ev_label:
                        act["label"] = ev_label
                    act["clicks"] += 1
                log_item = {
                    "ts": now,
                    "user_id": uid,
                    "type": "click",
                    "key": ev_key,
                    "label": ev_label,
                    "platform": u_entry["platform"],
                }
                events_log.append(log_item)
                pending_events.append(log_item)
            accepted += 1

        if accepted > 0:
            _ANALYTICS_STATE["dirty"] = True

    return {"ok": True, "accepted": accepted}


def _collect_analytics_dataset() -> Dict[str, Any]:
    """Собирает единый структурированный снимок статистики для краткого экрана и экспорта ИИ."""
    now = time.time()
    db_users_by_id: Dict[int, Dict[str, Any]] = {}
    try:
        for u in get_all_users() or []:
            if u and u.get("id"):
                db_users_by_id[int(u["id"])] = u
    except Exception:
        pass

    with _ANALYTICS_LOCK:
        users_snapshot = {k: dict(v) for k, v in _ANALYTICS_STATE["users"].items()}
        actions_snapshot = [dict(v) for v in _ANALYTICS_STATE["actions"].values()]
        events_snapshot = [dict(ev) for ev in _ANALYTICS_STATE["events"]]

    # Считаем уникальных пользователей на действие, активность по дням/часам и топ действий каждого ученика
    action_unique_users: Dict[str, set] = {}
    user_action_counts: Dict[int, Dict[str, int]] = {}
    hourly_map: Dict[str, Dict[str, int]] = {}
    daily_map: Dict[str, Dict[str, Any]] = {}
    first_event_ts = 0.0
    last_event_ts = 0.0

    for ev in events_snapshot:
        ts = float(ev.get("ts") or 0.0)
        uid = int(ev.get("user_id") or 0)
        ev_type = ev.get("type") or "click"
        ev_key = str(ev.get("key") or "")
        ev_label = str(ev.get("label") or ev_key)
        if ts > 0:
            if first_event_ts <= 0 or ts < first_event_ts:
                first_event_ts = ts
            if ts > last_event_ts:
                last_event_ts = ts
            dt_local = datetime.fromtimestamp(ts, tz=_SCHOOL_TZ)
            hour_label = dt_local.strftime("%H:00")
            day_label = dt_local.strftime("%Y-%m-%d")
            hb = hourly_map.setdefault(hour_label, {"hour": hour_label, "visits": 0, "clicks": 0})
            db_day = daily_map.setdefault(day_label, {"date": day_label, "visits": 0, "clicks": 0, "uids": set()})
            if uid > 0:
                db_day["uids"].add(uid)
            if ev_type == "visit":
                hb["visits"] += 1
                db_day["visits"] += 1
            elif ev_type == "click":
                hb["clicks"] += 1
                db_day["clicks"] += 1
        if ev_type == "click" and ev_key and uid > 0:
            action_unique_users.setdefault(ev_key, set()).add(uid)
            ua = user_action_counts.setdefault(uid, {})
            ua[ev_label] = ua.get(ev_label, 0) + 1

    all_uids = set(users_snapshot.keys()) | set(db_users_by_id.keys())
    users_list: List[Dict[str, Any]] = []
    platform_counts: Dict[str, int] = {}
    online_now = 0
    total_visits = 0
    total_clicks = 0
    active_users = 0

    def _resolve_identity(uid: int) -> tuple[str, str]:
        db_u = db_users_by_id.get(uid) or {}
        st = users_snapshot.get(uid) or {}
        name = (
            _clean_real_name(db_u.get("full_name") or "")
            or _clean_real_name(st.get("name") or "")
            or _clean_real_nick(db_u.get("telegram_nick") or "")
            or _clean_real_nick(st.get("nick") or "")
            or ("Гость (браузер)" if uid == 12345 else f"Ученик #{uid}")
        )
        nick = _clean_real_nick(db_u.get("telegram_nick") or "") or _clean_real_nick(st.get("nick") or "")
        return name, nick

    for uid in all_uids:
        st = users_snapshot.get(uid) or {}
        last_seen = float(st.get("last_seen") or 0.0)
        first_seen = float(st.get("first_seen") or 0.0)
        visits = int(st.get("visits") or 0)
        clicks = int(st.get("clicks") or 0)
        explicit_offline = bool(st.get("explicit_offline"))
        platform = st.get("platform") or ("TG Bot" if uid in db_users_by_id else "Web")

        if visits > 0 or clicks > 0:
            active_users += 1
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
        total_visits += visits
        total_clicks += clicks

        age = (now - last_seen) if last_seen > 0 else 9999999.0
        is_online = bool(last_seen > 0 and not explicit_offline and age <= 90)
        if is_online:
            online_now += 1

        display_name, display_nick = _resolve_identity(uid)
        top_user_actions = sorted(user_action_counts.get(uid, {}).items(), key=lambda x: -x[1])[:3]

        users_list.append({
            "user_id": uid,
            "name": display_name,
            "nick": display_nick,
            "platform": platform,
            "status": "online" if is_online else "offline",
            "visits": visits,
            "clicks": clicks,
            "first_seen": _fmt_local_dt(first_seen),
            "last_seen": _fmt_local_dt(last_seen),
            "last_action": st.get("last_action") or "",
            "top_actions": [{"label": lbl, "count": cnt} for lbl, cnt in top_user_actions],
        })

    users_list.sort(key=lambda u: (-(u["visits"] + u["clicks"]), 0 if u["status"] == "online" else 1, u["name"]))

    actions_snapshot.sort(key=lambda a: -int(a.get("clicks") or 0))
    sum_clicks = max(1, sum(int(a.get("clicks") or 0) for a in actions_snapshot))
    for act in actions_snapshot:
        c = int(act.get("clicks") or 0)
        act["share_pct"] = round((c / sum_clicks) * 100, 1)
        act["unique_users"] = len(action_unique_users.get(act.get("key") or "", set()))

    chronological_events: List[Dict[str, Any]] = []
    for ev in events_snapshot[-600:]:
        uid = int(ev.get("user_id") or 0)
        u_name, u_nick = _resolve_identity(uid)
        chronological_events.append({
            "time": _fmt_local_dt(float(ev.get("ts") or 0.0)),
            "user_id": uid,
            "name": u_name,
            "nick": u_nick,
            "type": ev.get("type") or "click",
            "key": ev.get("key") or "",
            "label": ev.get("label") or "",
            "platform": ev.get("platform") or "Web",
        })

    hourly_activity = [hourly_map[h] for h in sorted(hourly_map.keys())]
    daily_activity = [
        {
            "date": d,
            "unique_users": len(daily_map[d]["uids"]),
            "visits": daily_map[d]["visits"],
            "clicks": daily_map[d]["clicks"],
        }
        for d in sorted(daily_map.keys())
    ]

    sb_hist_raw = get_historical_activity(limit=150)
    sb_hist_users = [
        {
            "user_id": u["user_id"],
            "name": u["name"],
            "nick": u["nick"],
            "last_seen": _fmt_local_dt(float(u.get("last_seen") or 0.0)),
            "last_action": u.get("last_action") or "",
            "db_actions": int(u.get("db_actions") or 0),
        }
        for u in (sb_hist_raw.get("users_summary") or [])
    ]
    sb_hist_events = [
        {
            "time": _fmt_local_dt(float(ev.get("ts") or 0.0)),
            "user_id": ev["user_id"],
            "name": ev["name"],
            "nick": ev["nick"],
            "kind": ev.get("kind") or "action",
            "label": ev.get("label") or "",
        }
        for ev in (sb_hist_raw.get("events") or [])
    ]

    return {
        "ai_context": {
            "app": "Homework Portal (10-А)",
            "timezone": "Asia/Tashkent (UTC+5)",
            "note": "Usage analytics exported for AI product/engagement analysis: summary KPIs, clicked features with unique user reach, daily & hourly usage (UTC+5), per-student activity profiles, chronological event log, and historical database actions preserved in Supabase.",
        },
        "summary": {
            "generated_at": _fmt_local_dt(now),
            "period_start": _fmt_local_dt(first_event_ts) if first_event_ts > 0 else _fmt_local_dt(now),
            "period_end": _fmt_local_dt(last_event_ts) if last_event_ts > 0 else _fmt_local_dt(now),
            "online_now": online_now,
            "total_visits": total_visits,
            "total_clicks": total_clicks,
            "active_users": active_users,
            "registered_users": len(db_users_by_id),
            "log_events_count": len(events_snapshot),
            "platforms": platform_counts,
        },
        "top_actions": actions_snapshot,
        "daily_activity": daily_activity,
        "hourly_activity": hourly_activity,
        "users": users_list,
        "events": chronological_events,
        "supabase_history": {
            "users_summary": sb_hist_users,
            "events": sb_hist_events,
        },
    }


def _format_analytics_ai_txt(dataset: Dict[str, Any]) -> str:
    """Форматирует статистику в понятный структурированный отчёт (.txt/Markdown) специально для анализа ИИ."""
    s = dataset["summary"]
    lines = [
        "# Отчёт активности Homework Portal (10-А) для анализа ИИ",
        f"Сформировано: {s['generated_at']} (UTC+5)",
        f"Период событий: {s['period_start']} — {s['period_end']}",
        "",
        "Инструкция для ИИ: Проанализируй вовлечённость учеников, какие разделы/функции используют чаще всего и что почти не открывают, пиковые часы нагрузки, распределение платформ и предложи конкретные UX-улучшения.",
        "",
        "## 1. Общая сводка",
        f"- Сейчас онлайн: {s['online_now']}",
        f"- Всего заходов в приложение: {s['total_visits']}",
        f"- Всего кликов и действий: {s['total_clicks']}",
        f"- Активных учеников: {s['active_users']} из {s['registered_users']} в базе",
        f"- Записей в журнале событий: {s['log_events_count']}",
    ]
    if s.get("platforms"):
        plat_str = ", ".join(f"{k}: {v}" for k, v in s["platforms"].items())
        lines.append(f"- Платформы активных учеников: {plat_str}")

    lines.extend(["", "## 2. Куда чаще нажимают (разделы и кнопки)"])
    top_actions = dataset.get("top_actions") or []
    if not top_actions:
        lines.append("Кликов пока не зафиксировано.")
    else:
        lines.append("| № | Действие | Ключ | Кликов | Доля | Уникальных учеников |")
        lines.append("|---:|---|---|---:|---:|---:|")
        for idx, act in enumerate(top_actions, 1):
            lines.append(
                f"| {idx} | {act.get('label', '')} | {act.get('key', '')} | {act.get('clicks', 0)} | {act.get('share_pct', 0)}% | {act.get('unique_users', 0)} |"
            )

    daily = dataset.get("daily_activity") or []
    if daily:
        lines.extend(["", "## 3. Активность по дням (UTC+5)", "| Дата | Уникальных учеников | Заходов | Кликов |", "|---|---:|---:|---:|"])
        for d in daily:
            lines.append(f"| {d['date']} | {d['unique_users']} | {d['visits']} | {d['clicks']} |")

    hourly = dataset.get("hourly_activity") or []
    if hourly:
        lines.extend(["", "## 4. Активность по часам (UTC+5)", "| Час | Заходов | Кликов |", "|---|---:|---:|"])
        for h in hourly:
            lines.append(f"| {h['hour']} | {h['visits']} | {h['clicks']} |")

    active_list = [u for u in dataset.get("users") or [] if (u["visits"] > 0 or u["clicks"] > 0)]
    inactive_count = max(0, len(dataset.get("users") or []) - len(active_list))
    lines.extend(["", f"## 5. Активные ученики ({len(active_list)}, ещё не заходили за период: {inactive_count})"])
    if not active_list:
        lines.append("За этот период активных пользователей нет.")
    else:
        lines.append("| Ученик | Ник | Статус | Платформа | Заходы | Клики | Последняя активность | Топ действий |")
        lines.append("|---|---|---|---|---:|---:|---|---|")
        for u in active_list:
            nick_disp = f"@{u['nick'].lstrip('@')}" if u.get("nick") else f"ID {u['user_id']}"
            top_u = ", ".join(f"{x['label']} ({x['count']})" for x in u.get("top_actions") or []) or (u.get("last_action") or "—")
            lines.append(
                f"| {u['name']} | {nick_disp} | {u['status']} | {u['platform']} | {u['visits']} | {u['clicks']} | {u['last_seen']} | {top_u} |"
            )

    ev_list = (dataset.get("events") or [])[-400:]
    lines.extend(["", f"## 6. Хронология событий (последние {len(ev_list)})"])
    if not ev_list:
        lines.append("Журнал событий пуст.")
    else:
        for ev in ev_list:
            nick_disp = f"@{ev['nick'].lstrip('@')}, " if ev.get("nick") else ""
            lines.append(f"[{ev['time']}] {ev['name']} ({nick_disp}{ev['platform']}) | {ev['type']} | {ev['label']}")

    sb_hist = dataset.get("supabase_history") or {}
    sb_users = sb_hist.get("users_summary") or []
    sb_events = (sb_hist.get("events") or [])[-100:]
    if sb_users or sb_events:
        lines.extend(["", "## 7. История из постоянной БД (Supabase — сохраняется между деплоями)"])
        if sb_users:
            lines.append("| Ученик | Ник | Действий в БД | Последняя активность в БД | Последнее действие |")
            lines.append("|---|---|---:|---|---|")
            for u in sb_users:
                nick_disp = f"@{u['nick'].lstrip('@')}" if u.get("nick") else f"ID {u['user_id']}"
                lines.append(f"| {u['name']} | {nick_disp} | {u['db_actions']} | {u['last_seen']} | {u['last_action']} |")
        if sb_events:
            lines.extend(["", f"Последние события из БД ({len(sb_events)}):"])
            for ev in sb_events:
                nick_disp = f"@{ev['nick'].lstrip('@')}" if ev.get("nick") else f"ID {ev['user_id']}"
                lines.append(f"[{ev['time']}] {ev['name']} ({nick_disp}) | {ev['label']}")

    return "\n".join(lines) + "\n"


@app.get("/api/admin/analytics")
def get_admin_analytics(user_id: int = 0, admin_secret: Optional[str] = None):
    """Возвращает компактную сводку статистики и топ-5 действий для админ-окна."""
    if not _is_admin_user(user_id, admin_secret):
        raise HTTPException(status_code=403, detail="Admin access required")
    dataset = _collect_analytics_dataset()
    return {
        "status": "ok",
        "server_time": time.time(),
        "summary": dataset["summary"],
        "top_actions": dataset["top_actions"][:8],
    }


@app.get("/api/admin/analytics/export")
def export_admin_analytics(user_id: int = 0, format: str = "txt", admin_secret: Optional[str] = None):
    """Скачивание готового файла статистики (.txt для ИИ или .json) для анализа в нейросети."""
    if not _is_admin_user(user_id, admin_secret):
        raise HTTPException(status_code=403, detail="Admin access required")
    dataset = _collect_analytics_dataset()
    date_tag = datetime.now(_SCHOOL_TZ).strftime("%Y-%m-%d_%H-%M")
    fmt = (format or "txt").strip().lower()
    if fmt == "json":
        body = json.dumps(dataset, ensure_ascii=False, indent=2)
        filename = f"hw_analytics_{date_tag}.json"
        media_type = "application/json; charset=utf-8"
    else:
        body = _format_analytics_ai_txt(dataset)
        filename = f"hw_analytics_for_ai_{date_tag}.txt"
        media_type = "text/plain; charset=utf-8"

    return Response(
        content=body.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


class AdminExportChatRequest(BaseModel):
    user_id: int
    format: str = "txt"
    admin_secret: Optional[str] = None


@app.post("/api/admin/analytics/send-to-chat")
async def send_analytics_to_chat(req: AdminExportChatRequest):
    """Отправляет файл статистики напрямую в личный Telegram-чат администратора."""
    if not _is_admin_user(req.user_id, req.admin_secret):
        raise HTTPException(status_code=403, detail="Admin access required")

    dataset = await asyncio.to_thread(_collect_analytics_dataset)
    date_tag = datetime.now(_SCHOOL_TZ).strftime("%Y-%m-%d_%H-%M")
    fmt = (req.format or "txt").strip().lower()
    if fmt == "json":
        content = json.dumps(dataset, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"hw_analytics_{date_tag}.json"
        mime_type = "application/json"
    else:
        content = _format_analytics_ai_txt(dataset).encode("utf-8")
        filename = f"hw_analytics_for_ai_{date_tag}.txt"
        mime_type = "text/plain"

    s = dataset["summary"]
    caption = (
        f"📊 Статистика для ИИ ({s['generated_at']})\n"
        f"Заходов: {s['total_visits']} · Кликов: {s['total_clicks']} · "
        f"Активных: {s['active_users']}/{s['registered_users']} · Событий: {s['log_events_count']}"
    )
    return await _send_telegram_document(req.user_id, filename, content, mime_type, caption)


@app.post("/api/admin/analytics/reset")
async def reset_admin_analytics(user_id: int = 0, admin_secret: Optional[str] = None):
    """Очищает собранную статистику для начала нового периода измерений."""
    if not _is_admin_user(user_id, admin_secret):
        raise HTTPException(status_code=403, detail="Admin access required")
    with _ANALYTICS_LOCK:
        _ANALYTICS_STATE["users"].clear()
        _ANALYTICS_STATE["actions"].clear()
        _ANALYTICS_STATE["events"].clear()
        _ANALYTICS_STATE["pending_events"].clear()
        _ANALYTICS_STATE["dirty"] = False
    await asyncio.to_thread(clear_analytics_snapshot)
    return {"status": "ok"}
