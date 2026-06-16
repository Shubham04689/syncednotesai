import json
import logging
import asyncio
import re
import httpx
from typing import AsyncGenerator, Optional, Dict, Any

try:
    from backend.config import settings
    from backend.rate_limiter import key_manager
    from backend.database import get_note, save_note
except ModuleNotFoundError:
    from config import settings
    from rate_limiter import key_manager
    from database import get_note, save_note

# SDK imports
try:
    from groq import AsyncGroq
    GROQ_SDK_AVAILABLE = True
except ImportError:
    GROQ_SDK_AVAILABLE = False

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
    GOOGLE_GENAI_SDK_AVAILABLE = True
except ImportError:
    GOOGLE_GENAI_SDK_AVAILABLE = False

logger = logging.getLogger("SyncedNotesAI.Streaming")

PROVIDER_FALLBACK_CHAIN = ["groq", "gemini", "mistral"]
PROVIDER_DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
    "mistral": "mistral-small-latest",
}

def clean_json_response(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        s = "\n".join(lines[start:end])
    return s.strip()

def tokenize(text: str) -> list[str]:
    return re.findall(r'\w+', text.lower())

def extract_array_items(field_name: str, buffer: str) -> list[Any]:
    match = re.search(fr'"{field_name}"\s*:\s*\[', buffer)
    if not match:
        return []
    start_idx = match.end()
    
    items = []
    brace_count = 0
    bracket_count = 0
    item_start = -1
    in_string = False
    escape = False
    
    for idx in range(start_idx, len(buffer)):
        if idx >= len(buffer):
            break
        char = buffer[idx]
        if escape:
            escape = False
            continue
        if char == '\\':
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
            
        if not in_string:
            if char == '{':
                if brace_count == 0 and bracket_count == 0:
                    item_start = idx
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0 and bracket_count == 0 and item_start != -1:
                    item_str = buffer[item_start:idx+1]
                    try:
                        obj = json.loads(item_str)
                        items.append(obj)
                    except Exception:
                        pass
            elif char == '[':
                bracket_count += 1
            elif char == ']':
                if brace_count == 0 and bracket_count == 0:
                    break
                elif bracket_count > 0:
                    bracket_count -= 1
                    
    if not items and field_name in ["common_mistakes", "brainstorming_ideas"]:
        array_text_match = re.search(fr'"{field_name}"\s*:\s*\[(.*)', buffer, re.DOTALL)
        if array_text_match:
            array_text = array_text_match.group(1)
            items = []
            for m in re.finditer(r'"(.*?)(?<!\\)"', array_text):
                items.append(m.group(1))
    return items

def parse_partial_note_data(buffer: str) -> dict[str, Any]:
    res = {}
    for field in ["title", "summary", "tldr"]:
        m = re.search(fr'"{field}"\s*:\s*"(.*?)(?<!\\)"', buffer, re.DOTALL)
        if m:
            res[field] = m.group(1)
            
    for field in ["key_concepts", "sections", "formulas_and_rules", "practice_questions", "common_mistakes", "brainstorming_ideas"]:
        items = extract_array_items(field, buffer)
        if items or re.search(fr'"{field}"\s*:\s*\[', buffer):
            res[field] = items
    return res

async def stream_groq(model: str, key: str, prompt: str) -> AsyncGenerator[str, None]:
    if not GROQ_SDK_AVAILABLE:
        raise ImportError("Groq SDK is not installed")
    client = AsyncGroq(api_key=key)
    completion = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        response_format={"type": "json_object"},
        temperature=0.7,
        max_tokens=8192
    )
    async for chunk in completion:
        token = chunk.choices[0].delta.content or ""
        yield token

async def stream_gemini(model: str, key: str, prompt: str) -> AsyncGenerator[str, None]:
    if not GOOGLE_GENAI_SDK_AVAILABLE:
        raise ImportError("google-genai SDK is not installed")
    client = google_genai.Client(api_key=key)
    async for chunk in await client.aio.models.generate_content_stream(
        model=model,
        contents=prompt,
        config=google_genai_types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    ):
        yield chunk.text

async def stream_ollama(model: str, prompt: str) -> AsyncGenerator[str, None]:
    url = f"{settings.ollama_host}/api/generate"
    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream("POST", url, json={"model": model, "prompt": prompt, "stream": True, "format": "json"}) as response:
            response.raise_for_status()
            async for line in response.iter_lines():
                if line:
                    data = json.loads(line)
                    token = data.get("response", "")
                    yield token

async def call_mistral_direct(model: str, key: str, prompt: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

async def generate_llm_stream(provider: str, model: str, prompt: str) -> AsyncGenerator[Dict[str, Any], None]:
    provider = provider.lower()
    
    if provider == "ollama":
        try:
            yield {"type": "meta", "provider": "ollama", "model": model}
            async for token in stream_ollama(model, prompt):
                yield {"type": "token", "text": token}
            return
        except Exception as e:
            logger.error(f"Ollama stream error: {e}")
            yield {"type": "error", "message": f"Ollama error: {str(e)}"}
            return

    attempt_order = [(provider, model)]
    for fb in PROVIDER_FALLBACK_CHAIN:
        if fb != provider and key_manager.has_any_key(fb):
            fb_model = PROVIDER_DEFAULT_MODELS.get(fb, "")
            if fb_model:
                attempt_order.append((fb, fb_model))
                
    last_error = "No keys configured"
    
    for prov, mdl in attempt_order:
        key_list = key_manager.keys.get(prov, [])
        if not key_list:
            continue
            
        for attempt in range(len(key_list)):
            key = key_manager.get_key(prov)
            if not key:
                break
                
            try:
                if prov == "gemini":
                    yield {"type": "meta", "provider": "gemini", "model": mdl}
                    async for token in stream_gemini(mdl, key, prompt):
                        yield {"type": "token", "text": token}
                    return
                elif prov == "groq":
                    yield {"type": "meta", "provider": "groq", "model": mdl}
                    async for token in stream_groq(mdl, key, prompt):
                        yield {"type": "token", "text": token}
                    return
                elif prov == "mistral":
                    yield {"type": "meta", "provider": "mistral", "model": mdl}
                    res = await call_mistral_direct(mdl, key, prompt)
                    yield {"type": "token", "text": res}
                    return
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Streaming from [{prov}] failed: {e}")
                key_manager.mark_unhealthy(prov, key, duration=30.0)
                
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{settings.ollama_host}/api/tags")
            if r.status_code == 200:
                local_models = [m["name"] for m in r.json().get("models", [])]
                if local_models:
                    fallback_model = local_models[0]
                    yield {"type": "meta", "provider": "ollama", "model": fallback_model}
                    async for token in stream_ollama(fallback_model, prompt):
                        yield {"type": "token", "text": token}
                    return
    except Exception as e:
        last_error = str(e)
        
    yield {"type": "error", "message": f"All providers exhausted: {last_error}"}


async def generate_page_note_stream_sse(
    req: Any,
    request: Any
) -> AsyncGenerator[str, None]:
    # 1. Cache hit check
    cached = get_note(req.document_id, req.page_number)
    if cached:
        logger.info(f"Cache hit for streaming: doc={req.document_id} page={req.page_number}")
        try:
            notes_obj = json.loads(cached["note_data"])
            final_data = {
                "type": "final",
                "notes": notes_obj,
                "meta": {"provider": cached["provider"] + " (cached)", "model": cached["model"]}
            }
            yield f"data: {json.dumps(final_data)}\n\n"
            yield "data: [DONE]\n\n"
            return
        except Exception as e:
            logger.warning(f"Streaming cache parse error: {e} — regenerating")

    # 2. Build prompt
    try:
        from backend.main import build_notes_prompt, make_fallback_json
    except ModuleNotFoundError:
        from main import build_notes_prompt, make_fallback_json

    prompt = build_notes_prompt(req.text, req.page_number)
    
    buffer = ""
    used_provider = req.provider
    used_model = req.model
    
    emitted_fields = set()
    emitted_sections_count = 0
    
    try:
        async for event in generate_llm_stream(req.provider, req.model, prompt):
            # Check client disconnection
            if await request.is_disconnected():
                logger.info(f"Client disconnected mid-stream for doc={req.document_id} page={req.page_number}")
                break
                
            if event["type"] == "meta":
                used_provider = event["provider"]
                used_model = event["model"]
                # Stream the meta info to frontend
                yield f"data: {json.dumps({'type': 'meta', 'provider': used_provider, 'model': used_model})}\n\n"
                
            elif event["type"] == "token":
                token = event["text"]
                buffer += token
                yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"
                
                # Run regex extraction
                parsed = parse_partial_note_data(buffer)
                
                # Emit simple fields
                for field in ["title", "summary", "tldr"]:
                    if field in parsed and field not in emitted_fields:
                        yield f"data: {json.dumps({'type': 'field', 'field': field, 'value': parsed[field]})}\n\n"
                        emitted_fields.add(field)
                        
                # Emit list fields once their array is fully closed
                for field in ["key_concepts", "formulas_and_rules", "practice_questions", "common_mistakes", "brainstorming_ideas"]:
                    if field not in emitted_fields:
                        if re.search(fr'"{field}"\s*:\s*\[.*?\]', buffer, re.DOTALL):
                            yield f"data: {json.dumps({'type': 'field', 'field': field, 'value': parsed.get(field, [])})}\n\n"
                            emitted_fields.add(field)
                            
                # Emit new completed sections
                if "sections" in parsed:
                    sections = parsed["sections"]
                    while emitted_sections_count < len(sections):
                        sec = sections[emitted_sections_count]
                        yield f"data: {json.dumps({'type': 'section', 'index': emitted_sections_count, 'section': sec})}\n\n"
                        emitted_sections_count += 1
                        
            elif event["type"] == "error":
                yield f"data: {json.dumps({'type': 'error', 'message': event['message']})}\n\n"
                yield "data: [DONE]\n\n"
                return
                
        # Connection closed or generator finished
        disconnected = await request.is_disconnected()
        
        # Clean final buffer
        cleaned_buffer = clean_json_response(buffer)
        
        try:
            final_json = json.loads(cleaned_buffer)
            notes_obj = final_json.get("notes", final_json)
        except Exception as e:
            # Fallback parsing
            parsed_data = parse_partial_note_data(buffer)
            # Create a fallback note with parsed data
            notes_obj = make_fallback_json(req.page_number, cleaned_buffer[:500], f"JSON parse error: {str(e)}").get("notes")
            # Update with whatever we parsed
            for k, v in parsed_data.items():
                notes_obj[k] = v
                
        # If disconnected: save partial note only if title and summary are present
        if disconnected:
            if "title" in notes_obj and "summary" in notes_obj:
                try:
                    save_note(
                        doc_id=req.document_id,
                        page_number=req.page_number,
                        model=used_model,
                        provider=used_provider,
                        note_data=json.dumps(notes_obj),
                        mind_map="",
                        infographic=""
                    )
                    logger.info(f"Saved partial note for doc={req.document_id} page={req.page_number} after disconnect")
                except Exception as ex:
                    logger.error(f"Failed to save partial note: {ex}")
            return
            
        # Normal completion: save complete note
        try:
            save_note(
                doc_id=req.document_id,
                page_number=req.page_number,
                model=used_model,
                provider=used_provider,
                note_data=json.dumps(notes_obj),
                mind_map="",
                infographic=""
            )
        except Exception as ex:
            logger.error(f"Failed to cache final note: {ex}")
            
        # Emit final event
        final_data = {
            "type": "final",
            "notes": notes_obj,
            "meta": {"provider": used_provider, "model": used_model}
        }
        yield f"data: {json.dumps(final_data)}\n\n"
        yield "data: [DONE]\n\n"
        
    except Exception as e:
        logger.error(f"Error in generate_page_note_stream_sse: {e}", exc_info=True)
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield "data: [DONE]\n\n"
