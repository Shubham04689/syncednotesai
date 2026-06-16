from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional
import json
import os
from pathlib import Path
import logging

try:
    from backend.config import settings
    from backend.rate_limiter import key_manager
except ImportError:
    from config import settings
    from rate_limiter import key_manager

logger = logging.getLogger("SyncedNotesAI.KeyManagerAPI")
router = APIRouter(prefix="/api/keys")

class KeysPayload(BaseModel):
    gemini: List[str]
    groq: List[str]
    mistral: List[str]

class ProbeRequest(BaseModel):
    provider: str
    key: str

def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"

@router.get("")
async def get_keys():
    """Returns the list of configured keys (masked) for each provider."""
    return {
        "gemini": [mask_key(k) for k in settings.gemini_keys],
        "groq": [mask_key(k) for k in settings.groq_keys],
        "mistral": [mask_key(k) for k in settings.mistral_keys]
    }

@router.post("")
async def save_keys(payload: KeysPayload):
    """Saves keys to keys.json, reloads settings, and rebuilds key manager."""
    keys_json_path = Path(__file__).resolve().parent / "keys.json"
    
    gemini = [k.strip() for k in payload.gemini if k.strip()]
    groq = [k.strip() for k in payload.groq if k.strip()]
    mistral = [k.strip() for k in payload.mistral if k.strip()]
    
    data = {
        "gemini": gemini,
        "groq": groq,
        "mistral": mistral
    }
    
    try:
        with open(keys_json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            
        settings.reload_from_keys_json()
        
        key_manager.rebuild_provider("gemini", settings.gemini_keys)
        key_manager.rebuild_provider("groq", settings.groq_keys)
        key_manager.rebuild_provider("mistral", settings.mistral_keys)
        
        return {"status": "success", "message": "Keys saved and reloaded successfully"}
    except Exception as e:
        logger.error(f"Failed to save keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/probe")
async def probe_key(req: ProbeRequest):
    try:
        from backend.main import fetch_gemini_models, fetch_groq_models, fetch_mistral_models
    except ImportError:
        from main import fetch_gemini_models, fetch_groq_models, fetch_mistral_models

    """Probes a key using the cheap model-list endpoint."""
    provider = req.provider.lower()
    key = req.key.strip()
    
    if not key:
        raise HTTPException(status_code=400, detail="Key cannot be empty")
        
    if provider not in ["gemini", "groq", "mistral"]:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {req.provider}")
        
    try:
        if provider == "gemini":
            models = await fetch_gemini_models(key)
        elif provider == "groq":
            models = await fetch_groq_models(key)
        elif provider == "mistral":
            models = await fetch_mistral_models(key)
            
        if models:
            return {"valid": True, "message": f"Successfully validated key. Found {len(models)} models."}
        else:
            return {"valid": False, "message": "Key verification failed or returned no models."}
    except Exception as e:
        logger.warning(f"Key probe failed: {e}")
        return {"valid": False, "message": str(e)}
