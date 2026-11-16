from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List
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
    get_material_by_id,
    upsert_user,
    is_using_supabase
)
import uuid
import os
import base64

app = FastAPI()

if not os.path.exists("uploads"):
    os.makedirs("uploads", exist_ok=True)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

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
    results = []
    rows = get_pending_tasks(user_id)
    for row in rows:
        task_dict = dict(row)
        materials = get_materials_for_task(row['id'])
        task_dict['materials'] = [dict(m) for m in materials]
        results.append(task_dict)

    return results

@app.get("/api/materials/{material_id}/download")
@app.get("/api/materials/{material_id}")
def download_material(material_id: int):
    mat = get_material_by_id(material_id)
    if not mat:
        raise HTTPException(status_code=404, detail="Material not found")

    file_id = mat.get("file_id", "")
    caption = mat.get("caption") or f"homework_{material_id}.jpg"
    filename = caption if (caption.endswith(".jpg") or caption.endswith(".png") or caption.endswith(".jpeg")) else f"{caption}.jpg"

    if file_id.startswith("data:"):
        try:
            header, b64_data = file_id.split(",", 1)
            media_type = header.split(";")[0].replace("data:", "") or "image/jpeg"
            content = base64.b64decode(b64_data)
            return Response(
                content=content,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "public, max-age=31536000"
                }
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to decode image data: {e}")

    clean_path = file_id.lstrip("/")
    if os.path.exists(clean_path):
        return FileResponse(
            path=clean_path,
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    raise HTTPException(status_code=404, detail="File not found")

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
        for p in all_incoming:
            raw_name = p.filename or "photo.jpg"
            ext = os.path.splitext(raw_name)[1].lower()
            if not ext or ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"]:
                ext = ".jpg"
            
            unique_name = f"{task_id}_{uuid.uuid4().hex[:8]}{ext}"
            file_path = f"uploads/{unique_name}"
            
            contents = await p.read()
            # 1. Сохраняем на локальный диск
            try:
                with open(file_path, "wb") as f:
                    f.write(contents)
            except Exception as disk_err:
                print(f"Disk write warning: {disk_err}")

            # 2. Формируем универсальный Base64 Data URL для Supabase,
            # чтобы фото гарантированно отображались на ЛЮБОМ устройстве,
            # даже если Render спит, перезагружается или файлы стерты с временного диска.
            mime = "image/png" if ext == ".png" else "image/jpeg"
            b64_str = base64.b64encode(contents).decode("utf-8")
            data_url = f"data:{mime};base64,{b64_str}"

            storage_ref = data_url if is_using_supabase() else file_path

            batch_items.append({"file_id": storage_ref, "caption": raw_name})
            uploaded_results.append({"url": file_path, "name": raw_name})

        if batch_items:
            add_materials_batch(task_id, batch_items, user_id)

        # Возвращаем url первого файла для обратной совместимости + список всех
        first_url = uploaded_results[0]["url"] if uploaded_results else ""
        return {"status": "ok", "url": first_url, "uploaded": uploaded_results}
    except Exception as e:
        print(f"Error in /api/upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))
