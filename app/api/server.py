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
        is_using_supabase,
        get_notification_prefs,
        upsert_notification_prefs,
        get_leaderboard,
        cleanup_old_tasks,
        add_task_comment,
        get_task_comments,
        get_comments_count_batch,
        delete_task_comment
    )
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
        is_using_supabase,
        get_notification_prefs,
        upsert_notification_prefs,
        get_leaderboard,
        cleanup_old_tasks,
        add_task_comment,
        get_task_comments,
        get_comments_count_batch,
        delete_task_comment
    )
import uuid
import os
import json
import gzip
import io
import mimetypes
import base64
import hashlib
import asyncio
import logging
from urllib.parse import quote

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

# ─────────────────────────────────────────────────────────────
# ФОНОВАЯ АВТОМАТИЧЕСКАЯ ОЧИСТКА ЗАДАНИЙ СТАРШЕ 30 ДНЕЙ
# ─────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup_tasks():
    asyncio.create_task(_prewarm_backend_caches())
    asyncio.create_task(run_periodic_cleanup_loop())

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


@app.get("/api/materials/{material_id}/download")
@app.get("/api/materials/{material_id}")
def download_material(material_id: int, request: Request):
    # 1. Проверяем быструю RAM память (моментальная отдача за ~0-1 мс)
    cached = _MATERIAL_CACHE.get(material_id)

    # 2. Если нет в RAM, проверяем диск uploads/ (сначала метаданные документов, затем кэш фото)
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
                    etag = f'"{hashlib.md5(data).hexdigest()}"'
                    cached = {
                        "content": data,
                        "media_type": mime,
                        "etag": etag,
                        "filename": f"material_{material_id}{ext}"
                    }
                    _MATERIAL_CACHE[material_id] = cached
                    break
                except Exception:
                    pass

    # 3. Если нет в кэше, достаем из БД (Supabase / SQLite) 1 раз
    if not cached:
        mat = get_material_by_id(material_id)
        if not mat:
            raise HTTPException(status_code=404, detail="Material not found")

        # Проверяем, не является ли материал внешней ссылкой или документом [FILE]
        cap = mat.get("caption") or ""
        fid = mat.get("file_id") or ""
        if cap.startswith("[LINK]"):
            raw = cap[6:]
            link_url = raw.split("::", 1)[0].strip() if "::" in raw else raw.strip()
            return RedirectResponse(url=link_url, status_code=302)
        elif fid.startswith(("http://", "https://")):
            return RedirectResponse(url=fid, status_code=302)

        is_doc_file = cap.startswith("[FILE]")
        file_id = fid
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

        if file_id.startswith("data:"):
            try:
                header, b64_data = file_id.split(",", 1)
                parsed_mime = header.split(";")[0].replace("data:", "").strip()
                if is_doc_file:
                    media_type = guess_file_mime(filename, parsed_mime)
                else:
                    media_type = parsed_mime or "image/jpeg"
                content = base64.b64decode(b64_data)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to decode file data: {e}")
        else:
            clean_path = file_id.lstrip("/")
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

    # 4. Проверяем HTTP ETag (браузерный кэш 304 Not Modified без повторного скачивания)
    client_etag = request.headers.get("if-none-match")
    if client_etag and client_etag == cached["etag"]:
        return Response(
            status_code=304,
            headers={
                "ETag": cached["etag"],
                "Cache-Control": "public, max-age=2592000, immutable"
            }
        )

    # 5. Отдаем материал с правильным Content-Disposition и долгосрочным кэшем (30 дней)
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

class UserSync(BaseModel):
    user_id: int
    telegram_nick: Optional[str] = None
    full_name: Optional[str] = None

@app.post('/api/user/sync')
def sync_user(data: UserSync):
    existing = get_user(data.user_id)
    if existing and (
        (not data.telegram_nick or existing.get("telegram_nick") == data.telegram_nick)
        and (not data.full_name or existing.get("full_name") == data.full_name)
    ):
        return {"status": "ok", "role": existing.get("role", "student")}

    upsert_user(data.user_id, telegram_nick=data.telegram_nick, full_name=data.full_name)
    user_info = get_user(data.user_id) if not existing else existing
    role = user_info.get("role", "student") if isinstance(user_info, dict) else "student"
    return {"status": "ok", "role": role}

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
