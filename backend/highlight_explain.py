from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import logging

logger = logging.getLogger("SyncedNotesAI.HighlightExplain")
router = APIRouter(prefix="/api")

class ExplainRequest(BaseModel):
    text: str
    action: str  # 'explain', 'define', 'simplify', 'example'
    context: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None

def estimate_tokens(text: str) -> int:
    # Conservative estimate: 1 token ≈ 4 characters or 0.75 words.
    # We use words / 0.75.
    return int(len(text.split()) / 0.75)

@router.post("/explain")
async def explain_text(req: ExplainRequest):
    try:
        from backend.main import call_llm
    except ImportError:
        from main import call_llm

    action = req.action.lower()
    if action not in ["explain", "define", "simplify", "example"]:
        raise HTTPException(status_code=400, detail=f"Invalid action: {req.action}")
        
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Selected text cannot be empty")
        
    # Truncate text to 500 characters
    if len(text) > 500:
        text = text[:500] + "..."
        
    context = req.context.strip() if req.context else ""
    if len(context) > 800:
        context = context[:800] + "..."
        
    # Build prompt
    if action == "explain":
        prompt = f"Explain the following text in 2 to 3 sentences:\n\n{text}"
    elif action == "define":
        if context:
            prompt = f"Define the term '{text}' as used in this context: '{context}'."
        else:
            prompt = f"Define the term '{text}'."
    elif action == "simplify":
        prompt = f"Restate the following text in plain language suitable for a secondary school student:\n\n{text}"
    elif action == "example":
        prompt = f"Provide one concrete real-world example that illustrates this concept:\n\n{text}"
        
    # Ensure prompt is <= 200 tokens
    if estimate_tokens(prompt) > 200:
        allowed_words = 140
        words = text.split()
        if len(words) > allowed_words:
            text = " ".join(words[:allowed_words]) + "..."
            if action == "explain":
                prompt = f"Explain the following text in 2 to 3 sentences:\n\n{text}"
            elif action == "define":
                if context:
                    prompt = f"Define the term '{text}' as used in this context: '{context}'."
                else:
                    prompt = f"Define the term '{text}'."
            elif action == "simplify":
                prompt = f"Restate the following text in plain language suitable for a secondary school student:\n\n{text}"
            elif action == "example":
                prompt = f"Provide one concrete real-world example that illustrates this concept:\n\n{text}"

    # Determine provider and model (Prefer Groq)
    provider = req.provider or "groq"
    model = req.model
    if not model:
        if provider == "groq":
            model = "llama-3.3-70b-versatile"
        elif provider == "gemini":
            model = "gemini-2.0-flash"
        elif provider == "mistral":
            model = "mistral-small-latest"
        else:
            model = "llama-3.3-70b-versatile"
            
    try:
        raw_text, used_provider, used_model = await call_llm(
            provider=provider,
            model=model,
            prompt=prompt,
            expect_json=False
        )
        return {
            "explanation": raw_text.strip(),
            "action": action,
            "_meta": {
                "provider": used_provider,
                "model": used_model
            }
        }
    except Exception as e:
        logger.error(f"Error generating explanation: {e}")
        raise HTTPException(status_code=500, detail=str(e))
