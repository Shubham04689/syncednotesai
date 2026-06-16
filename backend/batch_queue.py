import json
import asyncio
import hashlib
import time
import os
import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

try:
    from backend.config import settings
    from backend.database import (
        get_db_connection, save_batch_job, get_batch_job, get_batch_jobs, update_batch_job
    )
except ImportError:
    from config import settings
    from database import (
        get_db_connection, save_batch_job, get_batch_job, get_batch_jobs, update_batch_job
    )

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

logger = logging.getLogger("SyncedNotesAI.BatchQueue")
router = APIRouter(prefix="/api/batch")

# ── WebSocket Connection Manager ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        # Immediately send snapshot of current jobs
        jobs = get_batch_jobs()
        await websocket.send_json({
            "type": "snapshot",
            "jobs": jobs
        })

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                if connection in self.active_connections:
                    self.active_connections.remove(connection)

ws_manager = ConnectionManager()

# ── Background Worker ─────────────────────────────────────────────────────────
class BatchWorker:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.is_running = False
        self.watchdog_observer = None

    async def start(self):
        self.is_running = True
        
        # 1. Load active jobs from DB and resume them
        jobs = get_batch_jobs()
        active_jobs = [j for j in jobs if j["status"] in ["queued", "processing"]]
        for job in active_jobs:
            logger.info(f"Resuming batch job {job['id']} ('{job['filename']}')")
            await self.queue.put(job["id"])
            
        # 2. Spawn worker loop
        asyncio.create_task(self.worker_loop())
        
        # 3. Spawn inbox watcher
        asyncio.create_task(self.start_inbox_watcher())

    async def add_job(self, job_id: int):
        await self.queue.put(job_id)

    async def worker_loop(self):
        while self.is_running:
            job_id = await self.queue.get()
            try:
                await self.process_job(job_id)
            except Exception as e:
                logger.error(f"Error running batch job {job_id}: {e}")
            finally:
                self.queue.task_done()

    async def process_job(self, job_id: int):
        try:
            from backend.database import (
                get_db_connection, get_batch_job, update_batch_job,
                save_document, get_document_by_hash
            )
            from backend.main import call_llm, build_notes_prompt, clean_json_response, save_note
            from backend.rate_limiter import key_manager
        except ImportError:
            from database import (
                get_db_connection, get_batch_job, update_batch_job,
                save_document, get_document_by_hash
            )
            from main import call_llm, build_notes_prompt, clean_json_response, save_note
            from rate_limiter import key_manager

        job = get_batch_job(job_id)
        if not job:
            return
            
        # Mark as processing
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_batch_job(job_id, status="processing", started_at=started_at)
        await ws_manager.broadcast({
            "type": "page_done",
            "job_id": job_id,
            "completed_pages": job.get("completed_pages", 0) or 0,
            "total_pages": job.get("total_pages", 0) or 0
        })
        
        file_path = job["file_path"]
        filename = job["filename"]
        provider = job["provider"]
        model = job["model"]
        
        if not os.path.exists(file_path):
            update_batch_job(job_id, status="failed", error_message="File not found")
            await ws_manager.broadcast({
                "type": "job_failed",
                "job_id": job_id,
                "error": "File not found"
            })
            return
            
        try:
            doc_id = job.get("document_id")
            if not doc_id:
                # Calculate sha256 hash
                sha256 = hashlib.sha256()
                with open(file_path, "rb") as f:
                    while chunk := f.read(8192):
                        sha256.update(chunk)
                file_hash = sha256.hexdigest()
                
                existing = get_document_by_hash(file_hash)
                if existing:
                    doc_id = existing["id"]
                    total_pages = len(existing["pages"])
                else:
                    if fitz is None:
                        raise ValueError("PyMuPDF is not installed; cannot parse PDF.")
                    doc = fitz.open(file_path)
                    total_pages = len(doc)
                    pages = []
                    for idx in range(total_pages):
                        page_num = idx + 1
                        page = doc[idx]
                        text = page.get_text()
                        pages.append({
                            "page_number": page_num,
                            "text": text
                        })
                    doc.close()
                    doc_id = save_document(file_hash, filename, pages)
                
                update_batch_job(job_id, status="processing", document_id=doc_id, total_pages=total_pages)
            else:
                total_pages = job["total_pages"]
                
            completed_pages = job.get("completed_pages", 0) or 0
            
            # Fetch page text
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT page_number, text FROM pages WHERE document_id = ?", (doc_id,))
                page_rows = cursor.fetchall()
                pages_dict = {r["page_number"]: r["text"] for r in page_rows}
            finally:
                conn.close()
                
            for page_num in range(1, total_pages + 1):
                # Check if note already exists
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT COUNT(*) FROM notes WHERE document_id = ? AND page_number = ?",
                        (doc_id, page_num)
                    )
                    has_note = cursor.fetchone()[0] > 0
                finally:
                    conn.close()
                    
                if has_note:
                    completed_pages = max(completed_pages, page_num)
                    update_batch_job(job_id, status="processing", completed_pages=completed_pages)
                    await ws_manager.broadcast({
                        "type": "page_done",
                        "job_id": job_id,
                        "completed_pages": completed_pages,
                        "total_pages": total_pages
                    })
                    continue
                    
                page_text = pages_dict.get(page_num, "").strip() or "Empty page"
                
                # Check rate limiting and block if necessary
                key = None
                while key is None:
                    key = key_manager.get_key(provider)
                    if not key:
                        wait = key_manager.seconds_until_available(provider)
                        logger.info(f"[Batch Queue] All keys for {provider} exhausted. Sleeping for {wait:.1f}s.")
                        await asyncio.sleep(wait)
                        
                prompt = build_notes_prompt(page_text, page_num)
                
                # Make the LLM call with a few retries
                retries = 3
                success = False
                err_msg = ""
                while retries > 0 and not success:
                    try:
                        raw, used_prov, used_mdl = await call_llm(provider, model, prompt)
                        raw = clean_json_response(raw)
                        parent_json = json.loads(raw)
                        notes_obj = parent_json.get("notes", parent_json)
                        
                        save_note(
                            doc_id=doc_id,
                            page_number=page_num,
                            model=used_mdl,
                            provider=used_prov,
                            note_data=json.dumps(notes_obj),
                            mind_map="",
                            infographic=""
                        )
                        success = True
                    except Exception as e:
                        retries -= 1
                        err_msg = str(e)
                        logger.warning(f"[Batch Queue] Processing failed for page {page_num}, retrying. Error: {e}")
                        await asyncio.sleep(2.0)
                        
                if not success:
                    update_batch_job(job_id, status="failed", error_message=f"Error on page {page_num}: {err_msg}")
                    await ws_manager.broadcast({
                        "type": "job_failed",
                        "job_id": job_id,
                        "error": f"Error on page {page_num}: {err_msg}"
                    })
                    return
                    
                completed_pages = page_num
                update_batch_job(job_id, status="processing", completed_pages=completed_pages)
                await ws_manager.broadcast({
                    "type": "page_done",
                    "job_id": job_id,
                    "completed_pages": completed_pages,
                    "total_pages": total_pages
                })
                
                # 2-second rate-limit spacing
                await asyncio.sleep(2.0)
                
            # Completed
            completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            update_batch_job(job_id, status="completed", completed_at=completed_at)
            await ws_manager.broadcast({
                "type": "job_done",
                "job_id": job_id,
                "filename": filename
            })
            
        except Exception as e:
            logger.error(f"Failed to process batch job {job_id}: {e}")
            update_batch_job(job_id, status="failed", error_message=str(e))
            await ws_manager.broadcast({
                "type": "job_failed",
                "job_id": job_id,
                "error": str(e)
            })

    async def start_inbox_watcher(self):
        inbox_dir = os.environ.get("INBOX_DIR") or str(Path(__file__).resolve().parent.parent / "inbox")
        uploads_dir = str(Path(__file__).resolve().parent.parent / "uploads")
        
        os.makedirs(inbox_dir, exist_ok=True)
        os.makedirs(uploads_dir, exist_ok=True)
        
        logger.info(f"Starting inbox watcher for directory: {inbox_dir}")
        
        if WATCHDOG_AVAILABLE:
            loop = asyncio.get_running_loop()
            worker = self
            
            class InboxHandler(FileSystemEventHandler):
                def on_created(self, event):
                    if event.is_directory or not event.src_path.lower().endswith(".pdf"):
                        return
                    asyncio.run_coroutine_threadsafe(
                        self.handle_new_file(event.src_path),
                        loop
                    )
                    
                async def handle_new_file(self, src_path):
                    # Wait for file to copy completely
                    await asyncio.sleep(2.0)
                    filename = os.path.basename(src_path)
                    dest_path = os.path.join(uploads_dir, filename)
                    if os.path.exists(dest_path):
                        base, ext = os.path.splitext(filename)
                        dest_path = os.path.join(uploads_dir, f"{base}_{int(time.time())}{ext}")
                        filename = os.path.basename(dest_path)
                        
                    try:
                        shutil.move(src_path, dest_path)
                        provider = "gemini" if settings.has_gemini else ("groq" if settings.has_groq else "ollama")
                        model = "gemini-2.0-flash" if provider == "gemini" else ("llama-3.3-70b-versatile" if provider == "groq" else "llama-3.1-8b-instant")
                        
                        job_id = save_batch_job(filename, dest_path, model, provider)
                        logger.info(f"[Watchdog] Queued batch job {job_id} for {filename}")
                        await worker.add_job(job_id)
                    except Exception as e:
                        logger.error(f"[Watchdog] Failed to process inbox file: {e}")
                        
            event_handler = InboxHandler()
            self.watchdog_observer = Observer()
            self.watchdog_observer.schedule(event_handler, inbox_dir, recursive=False)
            self.watchdog_observer.start()
            logger.info("Watchdog inbox watcher active.")
            
            while self.is_running:
                await asyncio.sleep(5.0)
        else:
            # Fallback to polling every 30 seconds
            logger.info("Watchdog not installed. Using polling inbox watcher (30s interval).")
            while self.is_running:
                try:
                    for file in os.listdir(inbox_dir):
                        if file.lower().endswith(".pdf"):
                            src_path = os.path.join(inbox_dir, file)
                            
                            # Stable size check
                            initial_size = os.path.getsize(src_path)
                            await asyncio.sleep(1.0)
                            if os.path.getsize(src_path) != initial_size:
                                continue
                                
                            dest_path = os.path.join(uploads_dir, file)
                            if os.path.exists(dest_path):
                                base, ext = os.path.splitext(file)
                                dest_path = os.path.join(uploads_dir, f"{base}_{int(time.time())}{ext}")
                                file = os.path.basename(dest_path)
                                
                            shutil.move(src_path, dest_path)
                            provider = "gemini" if settings.has_gemini else ("groq" if settings.has_groq else "ollama")
                            model = "gemini-2.0-flash" if provider == "gemini" else ("llama-3.3-70b-versatile" if provider == "groq" else "llama-3.1-8b-instant")
                            
                            job_id = save_batch_job(file, dest_path, model, provider)
                            logger.info(f"[Polling] Queued batch job {job_id} for {file}")
                            await self.add_job(job_id)
                except Exception as e:
                    logger.error(f"Error in polling inbox watcher: {e}")
                    
                await asyncio.sleep(30.0)

batch_worker = BatchWorker()

# ── API Endpoints ─────────────────────────────────────────────────────────────
class BatchJobPayload(BaseModel):
    filename: str
    file_path: str
    provider: str
    model: str

@router.get("/jobs")
async def get_jobs():
    """Returns all batch jobs."""
    return {"jobs": get_batch_jobs()}

@router.post("/job")
async def create_job(payload: BatchJobPayload):
    """Manually queues a new batch PDF processing job."""
    job_id = save_batch_job(
        filename=payload.filename,
        file_path=payload.file_path,
        model=payload.model,
        provider=payload.provider
    )
    logger.info(f"Manually queued batch job {job_id} for {payload.filename}")
    await batch_worker.add_job(job_id)
    return {"job_id": job_id}

@router.websocket("/ws/batch-progress")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint to broadcast progress events and snapshots."""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, listen for messages if needed
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
