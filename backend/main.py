import json
import logging
import hashlib
import base64
import fitz  # PyMuPDF
import httpx
import asyncio
from fastapi import FastAPI, UploadFile, File, HTTPException, Response, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from urllib.parse import quote as url_quote

try:
    from backend.config import settings
    from backend.rate_limiter import key_manager
    from backend.database import (
        init_db, get_document_by_hash, save_document, get_note, save_note,
        delete_note, get_document_hash, list_documents, get_document_by_id
    )
    from backend.streaming import generate_page_note_stream_sse
    from backend.flashcards import router as flashcards_router
    from backend.highlight_explain import router as explain_router
    from backend.key_manager_api import router as keys_router
    from backend.chat import router as chat_router
    from backend.document_intelligence import router as doc_intel_router
    from backend.synthesis import router as synthesis_router
    from backend.batch_queue import router as batch_router, websocket_endpoint as batch_ws_endpoint, batch_worker
except ModuleNotFoundError:
    from config import settings
    from rate_limiter import key_manager
    from database import (
        init_db, get_document_by_hash, save_document, get_note, save_note,
        delete_note, get_document_hash, list_documents, get_document_by_id
    )
    from streaming import generate_page_note_stream_sse
    from flashcards import router as flashcards_router
    from highlight_explain import router as explain_router
    from key_manager_api import router as keys_router
    from chat import router as chat_router
    from document_intelligence import router as doc_intel_router
    from synthesis import router as synthesis_router
    from batch_queue import router as batch_router, websocket_endpoint as batch_ws_endpoint, batch_worker

# ── Optional SDK imports (graceful degradation if not installed) ──────────────
try:
    from groq import AsyncGroq
    GROQ_SDK_AVAILABLE = True
except ImportError:
    GROQ_SDK_AVAILABLE = False

try:
    from mistralai.client import Mistral
    MISTRAL_SDK_AVAILABLE = True
except ImportError:
    try:
        from mistralai import Mistral  # type: ignore
        MISTRAL_SDK_AVAILABLE = True
    except ImportError:
        MISTRAL_SDK_AVAILABLE = False

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
    GOOGLE_GENAI_SDK_AVAILABLE = True
except ImportError:
    GOOGLE_GENAI_SDK_AVAILABLE = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SyncedNotesAI")

if GROQ_SDK_AVAILABLE:
    logger.info("Groq SDK (groq) loaded successfully.")
else:
    logger.info("Groq SDK not installed — falling back to httpx REST calls.")

if MISTRAL_SDK_AVAILABLE:
    logger.info("Mistral SDK (mistralai) loaded successfully.")
else:
    logger.info("Mistral SDK not installed — falling back to httpx REST calls.")

if GOOGLE_GENAI_SDK_AVAILABLE:
    logger.info("Google GenAI SDK (google-genai) loaded successfully.")
else:
    logger.info("Google GenAI SDK not installed — falling back to httpx REST calls.")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="SyncedNotes AI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(flashcards_router)
app.include_router(explain_router)
app.include_router(keys_router)
app.include_router(chat_router)
app.include_router(doc_intel_router)
app.include_router(synthesis_router)
app.include_router(batch_router)
app.add_api_websocket_route("/ws/batch-progress", batch_ws_endpoint)

from pathlib import Path
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"

discovered_local_models: List[str] = []

@app.on_event("startup")
async def startup_event():
    global discovered_local_models
    logger.info("Initializing local SQLite database and Ollama models...")
    try:
        init_db()
    except Exception as e:
        logger.error(f"Failed to initialize SQLite: {str(e)}")

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{settings.ollama_host}/api/tags")
            if response.status_code == 200:
                data = response.json()
                discovered_local_models = [m["name"] for m in data.get("models", [])]
                logger.info(f"Discovered Ollama models: {discovered_local_models}")
    except Exception as e:
        logger.warning(f"Could not connect to Ollama at {settings.ollama_host}: {str(e)}")

    # Start the batch worker
    try:
        await batch_worker.start()
        logger.info("Batch worker started successfully.")
    except Exception as e:
        logger.error(f"Failed to start batch worker: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Stopping batch worker...")
    try:
        batch_worker.is_running = False
        if batch_worker.watchdog_observer:
            batch_worker.watchdog_observer.stop()
            batch_worker.watchdog_observer.join()
        logger.info("Batch worker stopped.")
    except Exception as e:
        logger.error(f"Error stopping batch worker: {e}")


import time

# ─────────────────────────────────────────────────────────────────────────────
# Utility: SVG extractor
# ─────────────────────────────────────────────────────────────────────────────
def extract_svg(raw_str: str) -> str:
    if "[START_SVG]" in raw_str and "[END_SVG]" in raw_str:
        start_idx = raw_str.find("[START_SVG]") + len("[START_SVG]")
        end_idx = raw_str.find("[END_SVG]")
        raw_str = raw_str[start_idx:end_idx].strip()
    if "<svg" in raw_str and "</svg>" in raw_str:
        start_idx = raw_str.find("<svg")
        end_idx = raw_str.find("</svg>") + len("</svg>")
        return raw_str[start_idx:end_idx].strip()
    return raw_str.strip()


def resolve_image_model(available_models: List[str]) -> str:
    """Prefer banana → imagen → first available."""
    for m in available_models:
        if "banana" in m.lower():
            return m
    for m in available_models:
        if "imagen" in m.lower():
            return m
    if available_models:
        return available_models[0]
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Request model
# ─────────────────────────────────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    document_id: int
    page_number: int
    text: str
    model: str
    provider: str  # 'gemini', 'groq', 'mistral', 'ollama'
    image_model: Optional[str] = None
    image_provider: Optional[str] = None  # 'gemini', 'pollinations', 'huggingface', 'together', 'ollama'


# ─────────────────────────────────────────────────────────────────────────────
# Model discovery cache
# ─────────────────────────────────────────────────────────────────────────────
MODEL_CACHE: Dict[str, Any] = {
    "cloud": {},
    "local": [],
    "image_providers": [],
    "last_fetched": 0.0
}
CACHE_TTL = 300.0

MODEL_STATUS_CACHE: Dict[str, str] = {}
# Model status is NOT tested on startup or on a timer.
# Status is updated lazily: "ok" on first successful real call, "error" on repeated failures.
# This prevents burning free-tier quota on health-check pings.


def mark_model_ok(model: str):
    MODEL_STATUS_CACHE[model] = "ok"


def mark_model_error(model: str):
    MODEL_STATUS_CACHE[model] = "error"


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic model list fetchers — using SDKs when available, httpx fallback
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_gemini_models(key: str) -> List[str]:
    # Prefer the new google-genai SDK
    if GOOGLE_GENAI_SDK_AVAILABLE:
        try:
            client = google_genai.Client(api_key=key)
            result = []
            # list_models is a synchronous call, run in thread pool
            loop = asyncio.get_event_loop()
            models = await loop.run_in_executor(None, lambda: list(client.models.list()))
            for m in models:
                name = m.name
                if name.startswith("models/"):
                    name = name[len("models/"):]
                supported = getattr(m, "supported_actions", None) or []
                methods = getattr(m, "supported_generation_methods", supported)
                if "generateContent" in methods or "generateImages" in methods:
                    result.append(name)
            return sorted(list(set(result)))
        except Exception as e:
            logger.warning(f"google-genai SDK model list failed: {e}, falling back to httpx")

    # httpx fallback
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = {"X-goog-api-key": key}
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                models_data = response.json().get("models", [])
                res = []
                for m in models_data:
                    name = m.get("name", "")
                    if name.startswith("models/"):
                        name = name[len("models/"):]
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods or "generateImages" in methods:
                        res.append(name)
                return sorted(list(set(res)))
    except Exception as e:
        logger.warning(f"Error fetching Gemini models: {e}")
    return []


async def fetch_groq_models(key: str) -> List[str]:
    # Use Groq SDK if available
    if GROQ_SDK_AVAILABLE:
        try:
            groq_client = AsyncGroq(api_key=key)
            models_resp = await groq_client.models.list()
            return sorted([m.id for m in models_resp.data if m.id])
        except Exception as e:
            logger.warning(f"Groq SDK model list failed: {e}, falling back to httpx")

    # httpx fallback
    try:
        url = "https://api.groq.com/openai/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                return sorted([m.get("id") for m in response.json().get("data", []) if m.get("id")])
    except Exception as e:
        logger.warning(f"Error fetching Groq models: {e}")
    return []


async def fetch_mistral_models(key: str) -> List[str]:
    # Use Mistral SDK if available
    if MISTRAL_SDK_AVAILABLE:
        try:
            loop = asyncio.get_event_loop()
            m_client = Mistral(api_key=key)
            models_resp = await loop.run_in_executor(None, lambda: m_client.models.list())
            if models_resp and models_resp.data:
                return sorted([m.id for m in models_resp.data if m.id])
        except Exception as e:
            logger.warning(f"Mistral SDK model list failed: {e}, falling back to httpx")

    # httpx fallback
    try:
        url = "https://api.mistral.ai/v1/models"
        headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                return sorted([m.get("id") for m in response.json().get("data", []) if m.get("id")])
    except Exception as e:
        logger.warning(f"Error fetching Mistral models: {e}")
    return []


async def fetch_pollinations_image_models() -> List[str]:
    """Fetch available Pollinations image models — completely free, no auth needed."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://image.pollinations.ai/models")
            if response.status_code == 200:
                data = response.json()
                # Response is a list of model name strings
                if isinstance(data, list):
                    return [str(m) for m in data]
    except Exception as e:
        logger.warning(f"Could not fetch Pollinations models: {e}")
    # Reliable static fallback list (always present on Pollinations)
    return ["flux", "flux-realism", "flux-cablyai", "flux-anime", "flux-3d", "any-dark", "flux-pro", "turbo"]


# ─────────────────────────────────────────────────────────────────────────────
# Model status — lazy only (no ping tests, preserves free-tier quota)
# Status is promoted to "ok" by successful real calls and "error" by repeated
# 429/5xx failures inside the generation endpoint.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# /api/models
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/models")
async def get_models():
    """
    Returns available models. Model list is cached for CACHE_TTL seconds.
    Status indicators are updated lazily on real usage — no ping tests are fired.
    """
    global MODEL_CACHE
    now = time.time()

    # Reload Ollama list (local, no quota cost)
    local_models: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{settings.ollama_host}/api/tags")
            if r.status_code == 200:
                local_models = [m["name"] for m in r.json().get("models", [])]
                MODEL_CACHE["local"] = local_models
    except Exception:
        local_models = MODEL_CACHE.get("local", [])

    # Fetch Pollinations image models (no auth, no quota cost)
    pollinations_models = await fetch_pollinations_image_models()
    MODEL_CACHE["pollinations_image"] = pollinations_models

    # Return cached cloud models if still fresh
    cache_fresh = now - MODEL_CACHE["last_fetched"] < CACHE_TTL and MODEL_CACHE["cloud"]
    if cache_fresh:
        return {
            "cloud": MODEL_CACHE["cloud"],
            "local": local_models,
            "statuses": MODEL_STATUS_CACHE,
            "pollinations_image_models": pollinations_models,
            "image_providers": _build_image_providers(MODEL_CACHE["cloud"].get("gemini", []), pollinations_models)
        }

    # Fetch fresh model lists from provider APIs (list endpoints don't count against generation quota)
    cloud_models: Dict[str, List[str]] = {}

    if settings.has_gemini:
        for key in settings.gemini_keys:
            gm = await fetch_gemini_models(key)
            if gm:
                cloud_models["gemini"] = gm
                break

    if settings.has_groq:
        for key in settings.groq_keys:
            gm = await fetch_groq_models(key)
            if gm:
                cloud_models["groq"] = gm
                break

    if settings.has_mistral:
        for key in settings.mistral_keys:
            gm = await fetch_mistral_models(key)
            if gm:
                cloud_models["mistral"] = gm
                break

    MODEL_CACHE["cloud"] = cloud_models
    MODEL_CACHE["last_fetched"] = now

    # Pre-populate status cache as "unknown" for new models (no pings fired)
    for prov, models in cloud_models.items():
        for m in models:
            if m not in MODEL_STATUS_CACHE:
                MODEL_STATUS_CACHE[m] = "unknown"

    return {
        "cloud": cloud_models,
        "local": local_models,
        "statuses": MODEL_STATUS_CACHE,
        "pollinations_image_models": pollinations_models,
        "image_providers": _build_image_providers(cloud_models.get("gemini", []), pollinations_models)
    }


def _build_image_providers(gemini_models: List[str], pollinations_models: List[str]) -> List[Dict[str, Any]]:
    """Returns structured metadata about available image generation providers."""
    providers = []

    # Gemini Imagen / banana — best free tier (500 img/day, already keyed)
    imagen_models = [m for m in gemini_models if "imagen" in m.lower() or "banana" in m.lower()]
    if imagen_models:
        providers.append({
            "id": "gemini_imagen",
            "name": "Google Imagen (Gemini)",
            "description": "500 free images/day via Gemini API key. Best quality.",
            "models": imagen_models,
            "free": True,
            "requires_key": True
        })

    # Hugging Face — free tier with HF token
    if settings.has_huggingface:
        providers.append({
            "id": "huggingface",
            "name": "HuggingFace FLUX.1",
            "description": "FLUX.1-schnell (Apache 2.0). Free tier with HF account token.",
            "models": ["black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-xl-base-1.0"],
            "free": True,
            "requires_key": True
        })

    # Pollinations — free with account key
    providers.append({
        "id": "pollinations",
        "name": "Pollinations.AI",
        "description": "Free with account (enter.pollinations.ai). FLUX-based.",
        "models": pollinations_models,
        "free": True,
        "requires_key": True  # Now requires API key for image generation
    })

    return providers


@app.get("/v1/models")
async def get_v1_models():
    models_res = await get_models()
    openai_models = []
    for m in models_res.get("cloud", {}).get("gemini", []):
        openai_models.append({"id": m, "object": "model", "created": int(time.time()), "owned_by": "google"})
    for m in models_res.get("cloud", {}).get("groq", []):
        openai_models.append({"id": m, "object": "model", "created": int(time.time()), "owned_by": "groq"})
    for m in models_res.get("cloud", {}).get("mistral", []):
        openai_models.append({"id": m, "object": "model", "created": int(time.time()), "owned_by": "mistral"})
    for m in models_res.get("local", []):
        openai_models.append({"id": m, "object": "model", "created": int(time.time()), "owned_by": "ollama"})
    return {"object": "list", "data": openai_models}


# ─────────────────────────────────────────────────────────────────────────────
# Document & cache management endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.delete("/api/note/{doc_id}/{page_number}")
async def delete_note_cache(doc_id: int, page_number: int):
    try:
        deleted = delete_note(doc_id, page_number)
        return {"deleted": deleted, "doc_id": doc_id, "page_number": page_number}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear cache: {str(e)}")


@app.get("/api/documents")
async def get_documents():
    try:
        return list_documents()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query failed: {str(e)}")


@app.get("/api/document/{doc_id}")
async def get_document(doc_id: int):
    doc = get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return doc


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Uploaded file must be a PDF.")
    try:
        contents = await file.read()
        sha256 = hashlib.sha256()
        sha256.update(contents)
        doc_hash = sha256.hexdigest()

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        file_path = UPLOAD_DIR / f"{doc_hash}.pdf"
        with open(file_path, "wb") as f:
            f.write(contents)

        existing_doc = get_document_by_hash(doc_hash)
        if existing_doc:
            return {"document_id": existing_doc["id"], "pages": existing_doc["pages"], "cached": True}

        doc = fitz.open(stream=contents, filetype="pdf")
        pages_data = [
            {"page_number": i + 1, "text": page.get_text().strip()}
            for i, page in enumerate(doc)
        ]

        doc_id = save_document(doc_hash, file.filename, pages_data)
        return {"document_id": doc_id, "pages": pages_data, "cached": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse PDF: {str(e)}")


@app.get("/api/document/{doc_id}/page/{page_number}/image")
async def get_page_image(doc_id: int, page_number: int):
    try:
        doc_hash = get_document_hash(doc_id)
        if not doc_hash:
            raise HTTPException(status_code=404, detail="Document not found.")
        file_path = UPLOAD_DIR / f"{doc_hash}.pdf"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="PDF file not found on disk.")

        doc = fitz.open(str(file_path))
        page_idx = page_number - 1
        if page_idx < 0 or page_idx >= len(doc):
            raise HTTPException(status_code=400, detail="Invalid page number.")

        page = doc[page_idx]
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        return Response(content=pix.tobytes("png"), media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to render page: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# Text generation callers
# ─────────────────────────────────────────────────────────────────────────────
async def call_gemini(model: str, key: str, prompt: str) -> str:
    """Call Gemini text generation — SDK first, then httpx REST fallback."""
    if GOOGLE_GENAI_SDK_AVAILABLE:
        try:
            loop = asyncio.get_event_loop()
            client = google_genai.Client(api_key=key)
            resp = await loop.run_in_executor(None, lambda: client.models.generate_content(
                model=model,
                contents=prompt,
                config=google_genai_types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            ))
            mark_model_ok(model)
            key_manager.mark_ok("gemini", key)
            return resp.text
        except Exception as e:
            logger.warning(f"google-genai SDK call failed for {model}: {e}, falling back to httpx")

    # httpx fallback
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers={"Content-Type": "application/json", "X-goog-api-key": key}, json=body)
        if response.status_code == 429:
            raise httpx.HTTPStatusError("Rate Limit", request=response.request, response=response)
        response.raise_for_status()
        mark_model_ok(model)
        key_manager.mark_ok("gemini", key)
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


async def call_groq(model: str, key: str, prompt: str) -> str:
    """Call Groq — SDK first, then httpx REST fallback."""
    if GROQ_SDK_AVAILABLE:
        try:
            groq_client = AsyncGroq(api_key=key)
            resp = await groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=8192
            )
            mark_model_ok(model)
            key_manager.mark_ok("groq", key)
            return resp.choices[0].message.content
        except Exception as e:
            if "rate" in str(e).lower() or "429" in str(e):
                raise httpx.HTTPStatusError("Rate Limit", request=None, response=None)
            logger.warning(f"Groq SDK call failed for {model}: {e}, falling back to httpx")

    # httpx fallback
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        )
        if response.status_code == 429:
            raise httpx.HTTPStatusError("Rate Limit", request=response.request, response=response)
        response.raise_for_status()
        mark_model_ok(model)
        key_manager.mark_ok("groq", key)
        return response.json()["choices"][0]["message"]["content"]


async def call_mistral(model: str, key: str, prompt: str) -> str:
    """Call Mistral — SDK first, then httpx REST fallback."""
    if MISTRAL_SDK_AVAILABLE:
        try:
            loop = asyncio.get_event_loop()
            m_client = Mistral(api_key=key)
            resp = await loop.run_in_executor(None, lambda: m_client.chat.complete(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=8192,
                temperature=0.7
            ))
            mark_model_ok(model)
            key_manager.mark_ok("mistral", key)
            return resp.choices[0].message.content
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                raise httpx.HTTPStatusError("Rate Limit", request=None, response=None)
            logger.warning(f"Mistral SDK call failed for {model}: {e}, falling back to httpx")

    # httpx fallback
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        )
        if response.status_code == 429:
            raise httpx.HTTPStatusError("Rate Limit", request=response.request, response=response)
        response.raise_for_status()
        mark_model_ok(model)
        key_manager.mark_ok("mistral", key)
        return response.json()["choices"][0]["message"]["content"]


async def call_ollama(model: str, prompt: str) -> str:
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            f"{settings.ollama_host}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"}
        )
        response.raise_for_status()
        return response.json()["response"]


# ─────────────────────────────────────────────────────────────────────────────
# Image generation callers (in priority order)
# ─────────────────────────────────────────────────────────────────────────────
async def call_gemini_imagen(model: str, key: str, prompt: str) -> str:
    """Gemini Imagen / banana multimodal → base64 data URI."""
    is_banana = "banana" in model.lower()

    if "imagen" in model.lower() and not is_banana:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateImages"
        body = {"prompt": prompt, "numberOfImages": 1, "outputMimeType": "image/jpeg", "aspectRatio": "16:9"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers={"Content-Type": "application/json", "X-goog-api-key": key}, json=body)
            r.raise_for_status()
            data = r.json()
            b64 = data["generatedImages"][0]["image"]["imageBytes"]
            return f"data:image/jpeg;base64,{b64}"
    else:
        # banana / multimodal content generation with IMAGE modality
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]}
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers={"Content-Type": "application/json", "X-goog-api-key": key}, json=body)
            r.raise_for_status()
            parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
            for part in parts:
                inline = part.get("inlineData", {})
                if inline.get("data"):
                    mime = inline.get("mimeType", "image/jpeg")
                    return f"data:{mime};base64,{inline['data']}"
        raise ValueError(f"No image inlineData found for model {model}")


async def call_pollinations_image(prompt: str, model: str = "flux", width: int = 1024, height: int = 576) -> str:
    """
    Pollinations.AI image generation — completely FREE, no API key required.
    Returns a direct image URL (or base64 if we download it).
    Endpoint: https://image.pollinations.ai/prompt/{encoded_prompt}?model={model}&width={w}&height={h}&nologo=true
    """
    # Use GET endpoint — returns image bytes directly
    encoded_prompt = url_quote(prompt)
    params = f"?model={model}&width={width}&height={height}&nologo=true&enhance=true&seed={int(time.time()) % 9999}"
    
    # Add key if configured (for higher rate limits)
    if settings.pollinations_key:
        params += f"&token={settings.pollinations_key}"
    
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}{params}"
    
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(url)
        if response.status_code == 200:
            content_type = response.headers.get("content-type", "image/jpeg")
            mime = content_type.split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            b64 = base64.b64encode(response.content).decode("utf-8")
            return f"data:{mime};base64,{b64}"
        else:
            raise ValueError(f"Pollinations returned HTTP {response.status_code}")


async def call_huggingface_image(prompt: str, model: str = "black-forest-labs/FLUX.1-schnell") -> str:
    """
    Hugging Face Inference API — free tier with HF token.
    FLUX.1-schnell is permissively licensed (Apache 2.0) and fast.
    """
    if not settings.huggingface_token:
        raise ValueError("No HuggingFace token configured")

    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {settings.huggingface_token}", "Content-Type": "application/json"}
    body = {
        "inputs": prompt,
        "parameters": {
            "guidance_scale": 3.5,
            "num_inference_steps": 4,  # FLUX schnell works great at 4 steps
            "width": 1024,
            "height": 576
        }
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(url, headers=headers, json=body)
        if r.status_code == 503:
            # Model is loading — wait and retry once
            await asyncio.sleep(20)
            r = await client.post(url, headers=headers, json=body)
        if r.status_code == 200:
            content_type = r.headers.get("content-type", "image/jpeg")
            mime = content_type.split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            b64 = base64.b64encode(r.content).decode("utf-8")
            return f"data:{mime};base64,{b64}"
        r.raise_for_status()
    raise ValueError("HuggingFace image generation failed")


async def generate_infographic_image(prompt: str, imagen_model: str, gemini_keys: List[str]) -> Optional[str]:
    """
    Tries image generation providers in priority order:
    1. Google Gemini Imagen / banana (free tier: ~500 req/day — uses keys already configured)
    2. Hugging Face Inference API (free tier with HF token, FLUX.1-schnell)
    3. Pollinations.AI (free with API key; requires account at enter.pollinations.ai)
    Returns base64 data URI or None on total failure.
    """
    refined = prompt
    if "educational" not in refined.lower() and "diagram" not in refined.lower():
        refined = f"Educational infographic diagram, clean minimalist style, professional: {refined}"

    # ── Priority 1: Gemini Imagen / banana (best free: 500/day) ──────────────
    if gemini_keys and imagen_model:
        for key in gemini_keys:
            try:
                logger.info(f"Trying Gemini image model: {imagen_model}")
                img_prompt = f"Clean monochromatic educational infographic, vector style, dark charcoal on white background: {prompt}"
                result = await call_gemini_imagen(imagen_model, key, img_prompt)
                logger.info("Gemini image generation succeeded.")
                return result
            except Exception as e:
                logger.warning(f"Gemini Imagen failed with key: {e}")
                key_manager.mark_unhealthy("gemini", key, duration=120.0)

    # ── Priority 2: HuggingFace FLUX.1-schnell (free tier) ────────────────────
    if settings.has_huggingface:
        try:
            logger.info("Trying HuggingFace FLUX.1-schnell image generation...")
            result = await call_huggingface_image(refined, model="black-forest-labs/FLUX.1-schnell")
            logger.info("HuggingFace image generation succeeded.")
            return result
        except Exception as e:
            logger.warning(f"HuggingFace image generation failed: {e}")

    # ── Priority 3: Pollinations.AI (requires API key from enter.pollinations.ai) ─
    if settings.pollinations_key:
        try:
            logger.info("Trying Pollinations.AI image generation (API key)...")
            result = await call_pollinations_image(refined, model="flux", width=1024, height=576)
            logger.info("Pollinations.AI image generation succeeded.")
            return result
        except Exception as e:
            logger.warning(f"Pollinations image generation failed: {e}")

    logger.warning("All image generation providers failed.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────
def build_notes_prompt(text: str, page_number: int) -> str:
    """
    Rich notes prompt — maximises information density and learning value.
    Uses a structured JSON schema that the frontend renders with full formatting.
    """
    return f"""You are a world-class academic tutor and expert note-taker. Your job is to transform raw document text into the richest, most useful study notes possible.

Respond with ONLY valid JSON — no markdown fences, no preamble, no extra text outside the JSON.

{{
  "notes": {{
    "title": "Precise, descriptive title that captures exactly what this page covers (not generic like 'Introduction')",
    
    "summary": "A dense 3-5 sentence executive summary. Cover: (1) what the page is fundamentally about, (2) the core argument or finding, (3) why it matters, (4) how it connects to broader context.",
    
    "key_concepts": [
      {{
        "term": "Technical term or concept name",
        "definition": "Precise, complete definition in your own words — not copied from text",
        "example": "A concrete real-world or numerical example that makes this click"
      }}
    ],
    
    "sections": [
      {{
        "heading": "Specific subtopic heading — use the actual subject matter, not 'Section 1'",
        "points": [
          "★ CRITICAL: Each bullet must be a complete, self-contained insight — minimum 15 words. Include the WHY behind each fact, not just the fact itself.",
          "★ Be specific: use exact numbers, names, formulas, dates, thresholds when they appear in the text.",
          "★ Show relationships: explain how this point connects to other concepts.",
          "★ Aim for 4-7 substantive bullets per section — depth over breadth."
        ],
        "code_blocks": [
          {{
            "language": "python",
            "code": "# ONLY include if actual code/pseudocode/algorithms appear in the source text.\\n# If no code exists on this page, set code_blocks to an empty array []."
          }}
        ],
        "comparison_table": {{
          "caption": "Table caption describing what is being compared (or null if no comparison exists)",
          "headers": ["Column A", "Column B", "Column C"],
          "rows": [
            ["Value A1", "Value B1", "Value C1"],
            ["Value A2", "Value B2", "Value C2"]
          ]
        }}
      }}
    ],
    
    "formulas_and_rules": [
      {{
        "name": "Name of the formula, law, or rule",
        "expression": "The formula itself — use plain text math: e.g. E = mc^2, P(A|B) = P(A∩B)/P(B)",
        "variables": "What each variable/term means",
        "when_to_use": "The specific condition or scenario where this applies"
      }}
    ],
    
    "practice_questions": [
      {{
        "difficulty": "basic",
        "question": "A factual recall question testing core definitions or facts from this page.",
        "answer": "Complete answer with the specific fact, number, or definition — cite the page content directly."
      }},
      {{
        "difficulty": "intermediate",
        "question": "An application question: 'Given X, how/why would you...' or 'Explain the difference between...'",
        "answer": "A thorough answer that applies concepts — show the reasoning chain, not just the conclusion."
      }},
      {{
        "difficulty": "advanced",
        "question": "A synthesis question: 'Why does X lead to Y?', 'What would happen if...', 'Compare and contrast...'",
        "answer": "A nuanced answer that demonstrates deep understanding — include edge cases or caveats where relevant."
      }}
    ],
    
    "common_mistakes": [
      "A specific misconception or error students commonly make about this topic — explain why it's wrong and what's correct instead."
    ],
    
    "brainstorming_ideas": [
      "A concrete, specific real-world application of this material — name industries, products, or scenarios where this applies.",
      "A research question or open problem this page raises.",
      "A connection to another field or discipline that illuminates this concept."
    ],
    
    "tldr": "One-sentence TL;DR of the entire page — the single most important takeaway, written so clearly that anyone could understand it."
  }}
}}

QUALITY RULES — violating these produces poor notes:
1. NEVER write vague bullets like "This is important" or "This concept explains X" — always say WHY and HOW.
2. ALWAYS include specific numbers, names, and examples when they appear in the source.
3. comparison_table: include ONLY when the page actually compares multiple things — otherwise set the entire field to null.
4. formulas_and_rules: include ONLY when mathematical formulas, algorithms, or precise rules appear — otherwise set to empty array [].
5. key_concepts: extract 2-5 genuinely technical terms — not common words.
6. The goal is that a student reading ONLY these notes (not the original text) would fully understand the material.

--- PAGE {page_number} CONTENT ---
{text}
--- END OF PAGE ---
"""


def build_mindmap_prompt(title: str, summary: str, sections_text: str) -> str:
    """
    Mind map prompt — called on-demand only when the user clicks the Mind Map tab.
    Uses a condensed representation of the already-generated notes.
    """
    return f"""Generate a valid Mermaid flowchart mind map for the following topic.

Topic: {title}
Summary: {summary}
Key sections: {sections_text}

STRICT RULES — violations cause parse errors:
1. First line: graph TD
2. Node IDs: ONLY [A-Za-z0-9_], start with a letter. NO spaces, hyphens, dots.
3. Labels: ALWAYS double-quoted in brackets: nodeId["Label Text"]
4. Arrows: ALWAYS --> (two dashes + right angle). NEVER -> or →
5. 12-18 nodes total: 1 root → 3-5 branches → 2-3 leaves each
6. Keep labels SHORT (2-5 words)
7. Use varied shapes: nodeId["box"] | nodeId(("circle")) | nodeId{{"diamond"}}

Return ONLY the raw Mermaid syntax — no backticks, no explanation, nothing else.

Example of correct syntax:
graph TD
  root["Main Topic"]
  br_a["Branch A"]
  br_b["Branch B"]
  leaf_a1["Detail 1"]
  leaf_a2["Detail 2"]
  root --> br_a
  root --> br_b
  br_a --> leaf_a1
  br_a --> leaf_a2
"""


def build_infographic_prompt(title: str, summary: str, sections_text: str) -> str:
    """
    SVG infographic prompt — called on-demand only when the user clicks Infographic tab.
    """
    return f"""Generate a professional SVG educational infographic for the following content.

Title: {title}
Summary: {summary}
Sections: {sections_text}

RULES:
1. Return ONLY valid SVG XML wrapped between [START_SVG] and [END_SVG]
2. Open: <svg viewBox="0 0 800 500" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,-apple-system,sans-serif">
3. Close: </svg>
4. Colors: fill="var(--svg-fill)" stroke="var(--svg-stroke)" for panels; fill="var(--svg-accent)" for headings
5. Layout: title bar at top (y 0-60), 3 content panels side by side (y 80-420), footer (y 440-500)
6. Each panel: rounded rect rx="10", heading 14px bold, 3-4 bullet lines 11px, line spacing 18px
7. Well-formed XML: all tags closed, all attributes quoted, & escaped as &amp;
"""


def build_illustration_prompt(title: str, summary: str) -> str:
    return f"Clean educational infographic diagram, minimalist vector style, professional. Topic: {title}. {summary}"


# ─────────────────────────────────────────────────────────────────────────────
# JSON cleanup helpers
# ─────────────────────────────────────────────────────────────────────────────
def clean_json_response(raw: str) -> str:
    """Strip markdown fences and leading/trailing whitespace from LLM JSON output."""
    s = raw.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        # Strip first line (```json or ```) and last line (```)
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        s = "\n".join(lines[start:end])
    return s.strip()


def make_fallback_json(page_number: int, raw_content: str = "", error: str = "") -> Dict[str, Any]:
    return {
        "notes": {
            "title": f"Page {page_number}",
            "summary": error or "Could not parse generated content.",
            "sections": [{"heading": "Raw Output", "points": [raw_content[:500]] if raw_content else ["Generation failed"], "code_blocks": []}],
            "practice_questions": [],
            "brainstorming_ideas": []
        },
        "mind_map": "graph TD\n  root[\"Page Content\"]\n  err[\"Parse Error\"]\n  root --> err",
        "infographic": '<svg viewBox="0 0 800 400" xmlns="http://www.w3.org/2000/svg"><rect width="800" height="400" fill="var(--bg-secondary)"/><text x="400" y="200" text-anchor="middle" font-family="system-ui,sans-serif" font-size="16" fill="var(--text-muted)">Content unavailable</text></svg>',
        "illustration_prompt": ""
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main generation endpoint
# Provider fallback chain — tried in order when the requested provider is exhausted
PROVIDER_FALLBACK_CHAIN = ["groq", "gemini", "mistral"]

# Best text-generation model to use when falling back to a different provider
PROVIDER_DEFAULT_MODELS: Dict[str, str] = {
    "groq":    "llama-3.3-70b-versatile",
    "gemini":  "gemini-2.0-flash",
    "mistral": "mistral-small-latest",
}


async def _call_provider(provider: str, model: str, prompt: str) -> str:
    """Single call to one provider with one key. Raises on failure."""
    key = key_manager.get_key(provider)
    if not key:
        raise RuntimeError(f"No available key for {provider}")
    if provider == "gemini":
        return await call_gemini(model, key, prompt)
    elif provider == "groq":
        return await call_groq(model, key, prompt)
    elif provider == "mistral":
        return await call_mistral(model, key, prompt)
    raise ValueError(f"Unknown provider: {provider}")


async def call_llm(
    provider: str,
    model: str,
    prompt: str,
    expect_json: bool = True
) -> tuple[str, str, str]:
    """
    Routes a prompt to the right provider with automatic cross-provider fallback.

    Order:
      1. Requested provider (all its keys, round-robin)
      2. Any other configured cloud provider that still has quota
      3. Ollama (local fallback)

    Returns (raw_text, used_provider, used_model).
    Raises HTTPException(503) only if every option is exhausted.
    """
    provider = provider.lower()

    # ── Ollama — direct, no key rotation ──────────────────────────────────
    if provider == "ollama":
        try:
            return await call_ollama(model, prompt), "ollama", model
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Ollama error: {str(e)}")

    # ── Build the provider attempt order ──────────────────────────────────
    # Requested provider first, then others in fallback chain
    attempt_order: List[tuple[str, str]] = [(provider, model)]
    for fb in PROVIDER_FALLBACK_CHAIN:
        if fb != provider and key_manager.has_any_key(fb):
            fb_model = PROVIDER_DEFAULT_MODELS.get(fb, "")
            # Only add if we have a usable model for this provider
            cached_models = MODEL_CACHE["cloud"].get(fb, [])
            if not fb_model and cached_models:
                # Pick first non-image model
                fb_model = next(
                    (m for m in cached_models if "imagen" not in m and "banana" not in m),
                    cached_models[0]
                )
            if fb_model:
                attempt_order.append((fb, fb_model))

    last_error = "No providers configured"

    for prov, mdl in attempt_order:
        key_list = key_manager.keys.get(prov, [])
        if not key_list:
            continue

        # Try each key for this provider
        for attempt in range(len(key_list)):
            key = key_manager.get_key(prov)
            if not key:
                wait = key_manager.seconds_until_available(prov)
                logger.info(f"[{prov}] No key available (next in ~{wait:.0f}s). Skipping to next provider.")
                break  # move to next provider, don't retry exhausted keys

            try:
                if prov == "gemini":
                    res = await call_gemini(mdl, key, prompt)
                elif prov == "groq":
                    res = await call_groq(mdl, key, prompt)
                elif prov == "mistral":
                    res = await call_mistral(mdl, key, prompt)
                else:
                    continue

                if prov != provider:
                    logger.info(f"Used fallback provider [{prov}/{mdl}] instead of [{provider}/{model}]")
                return res, prov, mdl

            except httpx.HTTPStatusError as err:
                code = getattr(getattr(err, "response", None), "status_code", "N/A")
                duration = 300.0 if str(code) == "429" else 60.0
                last_error = f"HTTP {code}"
                logger.warning(f"[{prov}] Key rejected ({last_error}). Blocking for {duration:.0f}s.")
                key_manager.mark_unhealthy(prov, key, duration=duration)
                mark_model_error(mdl)
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[{prov}] Unexpected error: {e}")
                key_manager.mark_unhealthy(prov, key, duration=30.0)

    # ── Local Ollama fallback ──────────────────────────────────────────────
    logger.info(f"All cloud providers exhausted ({last_error}). Trying Ollama fallback…")
    local_models: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{settings.ollama_host}/api/tags")
            if r.status_code == 200:
                local_models = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass

    if local_models:
        fallback_model = local_models[0]
        logger.info(f"Ollama fallback: {fallback_model}")
        try:
            res = await call_ollama(fallback_model, prompt)
            return res, "ollama (fallback)", fallback_model
        except Exception as e:
            last_error = str(e)

    raise HTTPException(
        status_code=503,
        detail=(
            f"All providers are temporarily unavailable ({last_error}). "
            f"Groq wait: ~{key_manager.seconds_until_available('groq'):.0f}s, "
            f"Gemini wait: ~{key_manager.seconds_until_available('gemini'):.0f}s, "
            f"Mistral wait: ~{key_manager.seconds_until_available('mistral'):.0f}s. "
            "Please wait a moment and retry."
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Generate notes only (fast, cheap — auto-runs on page load)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/generate-page-note")
async def generate_page_note(req: GenerateRequest):
    """
    Phase 1 — notes only. Fast and cheap.
    Automatically called for every page when a document is opened.
    Mind map and infographic are generated separately on-demand.
    """
    # Cache hit
    cached = get_note(req.document_id, req.page_number)
    if cached:
        logger.info(f"Cache hit: doc={req.document_id} page={req.page_number}")
        try:
            return {
                "notes": json.loads(cached["note_data"]),
                "mind_map": cached["mind_map"] or "",
                "infographic": cached["infographic"] or "",
                "image_pending": False,
                "_meta": {"provider": cached["provider"] + " (cached)", "model": cached["model"]}
            }
        except Exception as e:
            logger.warning(f"Cache parse error: {e} — regenerating")

    # Build notes-only prompt
    prompt = build_notes_prompt(req.text, req.page_number)

    raw, used_provider, used_model = await call_llm(req.provider, req.model, prompt)
    raw = clean_json_response(raw)

    try:
        parent_json = json.loads(raw)
    except json.JSONDecodeError:
        parent_json = make_fallback_json(req.page_number, raw[:500], "JSON parse error")

    notes_obj = parent_json.get("notes", parent_json)  # handle if model returned notes directly

    # Persist — mind_map and infographic are empty strings until generated on-demand
    try:
        save_note(
            doc_id=req.document_id,
            page_number=req.page_number,
            model=used_model,
            provider=used_provider,
            note_data=json.dumps(notes_obj),
            mind_map="",
            infographic="",
        )
    except Exception as e:
        logger.error(f"Failed to cache note: {e}")

    return {
        "notes": notes_obj,
        "mind_map": "",
        "infographic": "",
        "image_pending": False,
        "_meta": {"provider": used_provider, "model": used_model}
    }


@app.post("/api/generate-page-note/stream")
async def generate_page_note_stream(req: GenerateRequest, request: Request):
    """
    SSE notes generation stream. Delivers NoteData progressively.
    """
    return StreamingResponse(
        generate_page_note_stream_sse(req, request),
        media_type="text/event-stream"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Generate mind map on-demand (called only when user clicks Mind Map tab)
# ─────────────────────────────────────────────────────────────────────────────
class MindMapRequest(BaseModel):
    document_id: int
    page_number: int
    model: str
    provider: str


@app.post("/api/generate-mindmap")
async def generate_mindmap(req: MindMapRequest):
    """
    On-demand mind map generation. Only called when the user opens the Mind Map tab.
    Reads cached notes to build the prompt — no re-reading the PDF text.
    """
    cached = get_note(req.document_id, req.page_number)
    if not cached:
        raise HTTPException(status_code=404, detail="No notes found for this page. Generate notes first.")

    # Return cached mind map if already generated
    if cached.get("mind_map"):
        return {"mind_map": cached["mind_map"]}

    # Build mind map from cached note content
    notes_obj = json.loads(cached["note_data"])
    sections_text = " | ".join([s.get("heading", "") for s in notes_obj.get("sections", [])])
    prompt = build_mindmap_prompt(
        title=notes_obj.get("title", ""),
        summary=notes_obj.get("summary", ""),
        sections_text=sections_text
    )

    raw, used_provider, used_model = await call_llm(req.provider, req.model, prompt, expect_json=False)

    # Clean fences from raw mermaid output
    mind_map_str = raw.strip()
    if mind_map_str.startswith("```"):
        lines = mind_map_str.splitlines()
        s = 1 if lines[0].startswith("```") else 0
        e = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        mind_map_str = "\n".join(lines[s:e]).strip()

    if not mind_map_str.startswith("graph") and not mind_map_str.startswith("flowchart"):
        mind_map_str = "graph TD\n  root[\"" + notes_obj.get("title", "Topic") + "\"]\n  err[\"Mind map generation failed\"]\n  root --> err"

    # Patch cache — update mind_map field only
    try:
        save_note(
            doc_id=req.document_id,
            page_number=req.page_number,
            model=cached["model"],
            provider=cached["provider"],
            note_data=cached["note_data"],
            mind_map=mind_map_str,
            infographic=cached.get("infographic", ""),
        )
    except Exception as e:
        logger.error(f"Failed to cache mind map: {e}")

    return {"mind_map": mind_map_str}


# ─────────────────────────────────────────────────────────────────────────────
# Generate infographic on-demand (called only when user opens Infographic tab)
# ─────────────────────────────────────────────────────────────────────────────
class InfographicRequest(BaseModel):
    document_id: int
    page_number: int
    model: str
    provider: str
    image_model: Optional[str] = None


IMAGE_JOB_STATUS: Dict[str, str] = {}


def _image_job_key(doc_id: int, page_number: int) -> str:
    return f"{doc_id}:{page_number}"


@app.post("/api/generate-infographic")
async def generate_infographic_endpoint(req: InfographicRequest):
    """
    On-demand infographic generation. Only called when the user opens the Infographic tab.
    Returns immediately with image_pending=True; image arrives via WebSocket.
    """
    cached = get_note(req.document_id, req.page_number)
    if not cached:
        raise HTTPException(status_code=404, detail="No notes found. Generate notes first.")

    # Return cached infographic if already generated
    if cached.get("infographic"):
        return {"infographic": cached["infographic"], "image_pending": False}

    notes_obj = json.loads(cached["note_data"])
    sections_text = " | ".join([s.get("heading", "") for s in notes_obj.get("sections", [])])

    # Resolve image model
    imagen_model = req.image_model or ""
    if not imagen_model and settings.has_gemini:
        imagen_model = resolve_image_model(MODEL_CACHE["cloud"].get("gemini", []))

    illustration_prompt = build_illustration_prompt(
        title=notes_obj.get("title", ""),
        summary=notes_obj.get("summary", "")
    )

    # Generate an SVG placeholder via the text LLM (fast, free)
    svg_prompt = build_infographic_prompt(
        title=notes_obj.get("title", ""),
        summary=notes_obj.get("summary", ""),
        sections_text=sections_text
    )
    svg_raw, _, _ = await call_llm(req.provider, req.model, svg_prompt, expect_json=False)
    placeholder_svg = extract_svg(svg_raw.strip()) if "<svg" in svg_raw else (
        '<svg viewBox="0 0 800 400" xmlns="http://www.w3.org/2000/svg">'
        '<rect width="800" height="400" fill="var(--bg-secondary)"/>'
        '<text x="400" y="200" text-anchor="middle" font-family="system-ui,sans-serif" '
        'font-size="14" fill="var(--text-muted)">Generating image…</text></svg>'
    )

    # Save SVG placeholder immediately
    try:
        save_note(
            doc_id=req.document_id,
            page_number=req.page_number,
            model=cached["model"],
            provider=cached["provider"],
            note_data=cached["note_data"],
            mind_map=cached.get("mind_map", ""),
            infographic=placeholder_svg,
        )
    except Exception as e:
        logger.error(f"Failed to cache placeholder infographic: {e}")

    # Fire background AI image generation
    asyncio.create_task(_run_image_generation_job(
        doc_id=req.document_id,
        page_number=req.page_number,
        illustration_prompt=illustration_prompt,
        notes_obj=notes_obj,
        current_infographic=placeholder_svg,
        imagen_model=imagen_model,
        used_model=cached["model"],
        used_provider=cached["provider"],
        mind_map_str=cached.get("mind_map", ""),
    ))

    return {"infographic": placeholder_svg, "image_pending": True}


async def _run_image_generation_job(
    doc_id: int,
    page_number: int,
    illustration_prompt: str,
    notes_obj: Dict[str, Any],
    current_infographic: str,
    imagen_model: str,
    used_model: str,
    used_provider: str,
    mind_map_str: str,
):
    """Background task: generate the AI image and patch the SQLite cache."""
    job_key = _image_job_key(doc_id, page_number)
    IMAGE_JOB_STATUS[job_key] = "pending"
    infographic_str = current_infographic

    try:
        img_result = await generate_infographic_image(illustration_prompt, imagen_model, settings.gemini_keys)
        if img_result:
            infographic_str = img_result

        save_note(
            doc_id=doc_id,
            page_number=page_number,
            model=used_model,
            provider=used_provider,
            note_data=json.dumps(notes_obj),
            mind_map=mind_map_str,
            infographic=infographic_str,
        )
        IMAGE_JOB_STATUS[job_key] = "done"
        logger.info(f"Image job done: doc={doc_id} page={page_number}")
    except Exception as e:
        logger.warning(f"Image job failed: doc={doc_id} page={page_number}: {e}")
        IMAGE_JOB_STATUS[job_key] = "failed"


@app.websocket("/ws/note-infographic/{doc_id}/{page_number}")
async def ws_note_infographic(websocket: WebSocket, doc_id: int, page_number: int):
    """WebSocket: sends one message when the background image job finishes."""
    await websocket.accept()
    job_key = _image_job_key(doc_id, page_number)
    try:
        while True:
            status = IMAGE_JOB_STATUS.get(job_key)
            if status == "done":
                cached = get_note(doc_id, page_number)
                await websocket.send_json({
                    "type": "infographic_ready",
                    "infographic": cached["infographic"] if cached else None,
                })
                break
            if status == "failed":
                await websocket.send_json({"type": "infographic_failed"})
                break
            await asyncio.sleep(0.4)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
