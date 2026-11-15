from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List
from db import mark_task_done, get_pending_tasks, add_task, add_material, add_materials_batch, get_materials_for_task, upsert_user, is_using_supabase
import uuid
import os

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
            with open(file_path, "wb") as f:
                f.write(contents)
            
            batch_items.append({"file_id": file_path, "caption": raw_name})
            uploaded_results.append({"url": file_path, "name": raw_name})

        if batch_items:
            add_materials_batch(task_id, batch_items, user_id)

        # Возвращаем url первого файла для обратной совместимости + список всех
        first_url = uploaded_results[0]["url"] if uploaded_results else ""
        return {"status": "ok", "url": first_url, "uploaded": uploaded_results}
    except Exception as e:
        print(f"Error in /api/upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))
