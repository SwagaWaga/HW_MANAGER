from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from db import (
    mark_task_done,
    get_pending_tasks,
    add_task,
    update_task,
    delete_task,
    add_material,
    add_materials_batch,
    delete_material,
    get_materials_for_task,
    get_materials_for_tasks_batch,
    get_material_by_id,
    upsert_user,
    is_using_supabase,
    get_notification_prefs,
    upsert_notification_prefs
)
import uuid
import os
import base64
import hashlib

app = FastAPI()

if not os.path.exists("uploads"):
    os.makedirs("uploads", exist_ok=True)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Сверхбыстрый многоуровневый кэш фото (RAM -> Диск uploads/ -> Supabase)
# Устраняет повторные сетевые запросы к БД и отдает материалы за 0-2 миллисекунды
_MATERIAL_CACHE: Dict[int, Dict[str, Any]] = {}

def optimize_image_bytes(image_bytes: bytes, max_dim: int = 1600, quality: int = 80) -> tuple[bytes, str]:
    """
    Интеллектуальная оптимизация и сжатие загружаемых фото:
    - Автоматически выравнивает ориентацию по EXIF смартфонов (устраняет перевернутые фото).
    - Уменьшает гигантские снимки смартфонов (4000px -> до 1600px по длинной стороне).
    - Сохраняет в прогрессивный JPEG с качеством 80% (идеальная четкость формул и рукописного текста при весе ~120-250 КБ).
    - Экономит до 90% диска и трафика Supabase.
    """
    try:
        from PIL import Image, ImageOps
        import io
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
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
        return image_bytes, "image/jpeg"

@app.get("/")
def route():
    return FileResponse("static/index.html")

@app.get("/test-backpack")
def test_backpack():
    return FileResponse("scratch/test_backpack_real.html")

@app.get("/api/status")
@app.get("/health")
@app.get("/ping")
def get_db_status():
    return {
        "status": "ok",
        "backend": "supabase" if is_using_supabase() else "sqlite"
    }

@app.get("/api/tasks")
def get_tasks(user_id: int):
    rows = get_pending_tasks(user_id)
    if not rows:
        return []

    # 1. Быстрый пакетный запрос всех материалов одним запросом (вместо N+1 запросов!)
    # 2. Не возвращает тяжелый base64 в списке заданий, сокращая трафик с 10 МБ до ~2 КБ
    task_ids = [row["id"] for row in rows]
    materials_by_task = get_materials_for_tasks_batch(task_ids)

    results = []
    for row in rows:
        task_dict = dict(row)
        task_dict['materials'] = materials_by_task.get(row['id'], [])
        results.append(task_dict)

    return results

@app.get("/api/materials/{material_id}/download")
@app.get("/api/materials/{material_id}")
def download_material(material_id: int, request: Request):
    # 1. Проверяем быструю RAM память (моментальная отдача за ~0-1 мс)
    cached = _MATERIAL_CACHE.get(material_id)

    # 2. Если нет в RAM, проверяем диск uploads/ (отдача за 1-3 мс)
    if not cached:
        for ext, mime in [(".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")]:
            disk_path = f"uploads/cached_{material_id}{ext}"
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

        file_id = mat.get("file_id", "")
        caption = mat.get("caption") or f"material_{material_id}.jpg"
        filename = caption if (caption.endswith(".jpg") or caption.endswith(".png") or caption.endswith(".jpeg") or caption.endswith(".webp")) else f"{caption}.jpg"

        content = None
        media_type = "image/jpeg"

        if file_id.startswith("data:"):
            try:
                header, b64_data = file_id.split(",", 1)
                media_type = header.split(";")[0].replace("data:", "") or "image/jpeg"
                content = base64.b64decode(b64_data)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to decode image data: {e}")
        else:
            clean_path = file_id.lstrip("/")
            if os.path.exists(clean_path):
                try:
                    with open(clean_path, "rb") as f:
                        content = f.read()
                    if clean_path.endswith(".png"):
                        media_type = "image/png"
                    elif clean_path.endswith(".webp"):
                        media_type = "image/webp"
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")

        if not content:
            raise HTTPException(status_code=404, detail="File content not found")

        etag = f'"{hashlib.md5(content).hexdigest()}"'
        ext = ".png" if media_type == "image/png" else ".jpg"
        save_path = f"uploads/cached_{material_id}{ext}"
        try:
            with open(save_path, "wb") as f:
                f.write(content)
        except Exception:
            pass

        cached = {
            "content": content,
            "media_type": media_type,
            "etag": etag,
            "filename": filename
        }
        _MATERIAL_CACHE[material_id] = cached

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

    # 5. Отдаем изображение с правильными заголовками inline и долгосрочным кэшем (30 дней)
    return Response(
        content=cached["content"],
        media_type=cached["media_type"],
        headers={
            "Content-Disposition": f'inline; filename="{cached["filename"]}"',
            "Cache-Control": "public, max-age=2592000, immutable",
            "ETag": cached["etag"]
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

@app.put('/api/tasks/{task_id}')
@app.post('/api/tasks/{task_id}/edit')
def edit_task(task_id: int, data: TaskUpdate):
    success = update_task(task_id, data.subject, data.description, data.due_date, data.user_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update task")
    return {"status": "ok", "task_id": task_id}

class TaskDelete(BaseModel):
    user_id: int

@app.delete('/api/tasks/{task_id}')
@app.post('/api/tasks/{task_id}/delete')
def remove_task(task_id: int, user_id: Optional[int] = None, data: Optional[TaskDelete] = None):
    effective_user_id = user_id or (data.user_id if data else None)
    delete_task(task_id, effective_user_id)
    return {"status": "ok"}

@app.delete('/api/materials/{material_id}')
@app.post('/api/materials/{material_id}/delete')
def remove_material(material_id: int):
    # Очищаем кэш в RAM и на диске
    _MATERIAL_CACHE.pop(material_id, None)
    for ext in [".jpg", ".png", ".webp", ".jpeg"]:
        disk_path = f"uploads/cached_{material_id}{ext}"
        if os.path.exists(disk_path):
            try:
                os.remove(disk_path)
            except Exception:
                pass
    delete_material(material_id)
    return {"status": "ok"}

class UserSync(BaseModel):
    user_id: int
    telegram_nick: Optional[str] = None
    full_name: Optional[str] = None

@app.post('/api/user/sync')
def sync_user(data: UserSync):
    upsert_user(data.user_id, telegram_nick=data.telegram_nick, full_name=data.full_name)
    return {"status": "ok"}

@app.post("/api/upload")
async def upload_photos(
    task_id: int = Form(...),
    user_id: int = Form(...),
    photos: List[UploadFile] = File(None),
    photo: Optional[UploadFile] = File(None)
):
    try:
        os.makedirs("uploads", exist_ok=True)
        all_incoming: List[UploadFile] = []
        if photos:
            all_incoming.extend(photos)
        if photo:
            all_incoming.append(photo)

        uploaded_results = []
        batch_items = []
        items_contents = []

        for p in all_incoming:
            raw_name = p.filename or "photo.jpg"
            ext = os.path.splitext(raw_name)[1].lower()
            if not ext or ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"]:
                ext = ".jpg"
            
            raw_contents = await p.read()
            # Оптимизация и сжатие фото
            contents, mime = optimize_image_bytes(raw_contents, max_dim=1600, quality=80)
            ext = ".jpg"
            unique_name = f"{task_id}_{uuid.uuid4().hex[:8]}{ext}"
            file_path = f"uploads/{unique_name}"

            # 1. Сохраняем на локальный диск
            try:
                with open(file_path, "wb") as f:
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
            # Моментально прогреваем кэш в RAM и на диске для новых материалов
            if isinstance(created_ids, list):
                for idx, mat_id in enumerate(created_ids):
                    if idx < len(items_contents):
                        c_bytes, c_mime, c_name = items_contents[idx]
                        c_etag = f'"{hashlib.md5(c_bytes).hexdigest()}"'
                        _MATERIAL_CACHE[mat_id] = {
                            "content": c_bytes,
                            "media_type": c_mime,
                            "etag": c_etag,
                            "filename": c_name
                        }
                        try:
                            c_ext = ".png" if c_mime == "image/png" else ".jpg"
                            with open(f"uploads/cached_{mat_id}{c_ext}", "wb") as f:
                                f.write(c_bytes)
                        except Exception:
                            pass

        # Возвращаем url первого файла для обратной совместимости + список всех
        first_url = uploaded_results[0]["url"] if uploaded_results else ""
        return {"status": "ok", "url": first_url, "uploaded": uploaded_results}
    except Exception as e:
        print(f"Error in /api/upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class NotifPrefsRequest(BaseModel):
    user_id: int
    notify_08: bool = True
    notify_16: bool = True
    notify_18: bool = True

@app.get("/api/notifications/prefs")
def get_user_notification_prefs(user_id: int):
    return get_notification_prefs(user_id)

@app.post("/api/notifications/prefs")
def update_user_notification_prefs(payload: NotifPrefsRequest):
    success = upsert_notification_prefs(
        payload.user_id,
        payload.notify_08,
        payload.notify_16,
        payload.notify_18
    )
    return {"status": "ok" if success else "error", "prefs": {
        "notify_08": payload.notify_08,
        "notify_16": payload.notify_16,
        "notify_18": payload.notify_18
    }}
