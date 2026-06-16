import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging

try:
    from backend.database import (
        get_db_connection, create_chat_session, get_chat_history,
        get_chat_session_by_doc, save_chat_message
    )
    from backend.embeddings import ensure_page_embeddings, find_top_k_pages
    from backend.streaming import generate_llm_stream
except ImportError:
    from database import (
        get_db_connection, create_chat_session, get_chat_history,
        get_chat_session_by_doc, save_chat_message
    )
    from embeddings import ensure_page_embeddings, find_top_k_pages
    from streaming import generate_llm_stream

logger = logging.getLogger("SyncedNotesAI.Chat")
router = APIRouter(prefix="/api/chat")

class ChatMessageRequest(BaseModel):
    session_id: int
    message: str
    provider: Optional[str] = None
    model: Optional[str] = None

@router.post("/session/{doc_id}")
async def get_or_create_session(doc_id: int):
    """Gets existing session or creates a new one for the document."""
    try:
        session_id = get_chat_session_by_doc(doc_id)
        if session_id is None:
            session_id = create_chat_session(doc_id)
        return {"session_id": session_id}
    except Exception as e:
        logger.error(f"Error creating chat session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history/{session_id}")
async def get_history(session_id: int):
    """Retrieves chat history for a session."""
    try:
        history = get_chat_history(session_id)
        return {"messages": history}
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/message")
async def send_chat_message(req: ChatMessageRequest, request: Request):
    """
    Handles user chat message, executes RAG retrieval, and streams back
    the reply using SSE. Saves both query and reply to database.
    """
    session_id = req.session_id
    query_text = req.message.strip()
    
    if not query_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
        
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT document_id FROM chat_sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        doc_id = row["document_id"]
        
        cursor.execute("SELECT page_number, text FROM pages WHERE document_id = ?", (doc_id,))
        page_rows = cursor.fetchall()
        pages = [{"page_number": r["page_number"], "text": r["text"]} for r in page_rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking session: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
        
    if not pages:
        raise HTTPException(status_code=400, detail="No pages found for this document. Cannot perform RAG.")
        
    await ensure_page_embeddings(doc_id, pages)
    
    top_pages = await find_top_k_pages(doc_id, query_text, k=3)
    cited_page_nums = [p["page_number"] for p in top_pages if p["similarity"] > 0.05]
    if not cited_page_nums:
        cited_page_nums = [top_pages[0]["page_number"]] if top_pages else []
        
    save_chat_message(session_id, "user", query_text)
    
    context_parts = []
    for p in top_pages:
        context_parts.append(f"Page {p['page_number']}:\n{p['text']}\n")
    context_block = "\n---\n".join(context_parts)
    
    history = get_chat_history(session_id)
    
    system_instruction = (
        "You are an AI study assistant. Answer the user's question about the document using the context provided below. "
        "Always cite the page number(s) you use (e.g. [Page X] or [Page X, Page Y]) when referencing specific facts. "
        "Be concise, informative, and direct in your answers."
    )
    
    prompt_messages = [f"System: {system_instruction}\nContext:\n{context_block}\n---"]
    for msg in history[:-1]:
        prompt_messages.append(f"{msg['role'].capitalize()}: {msg['content']}")
    prompt_messages.append(f"User: {query_text}")
    prompt_messages.append("Assistant:")
    
    prompt = "\n\n".join(prompt_messages)
    
    async def chat_stream_generator():
        full_reply = ""
        used_prov = req.provider or "gemini"
        used_mdl = req.model or "gemini-2.0-flash"
        
        yield f"data: {json.dumps({'type': 'citations', 'pages': cited_page_nums})}\n\n"
        
        try:
            async for chunk in generate_llm_stream(used_prov, used_mdl, prompt):
                if await request.is_disconnected():
                    logger.info("Client disconnected from chat stream")
                    break
                    
                if chunk["type"] == "token":
                    text = chunk["text"]
                    full_reply += text
                    yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"
                elif chunk["type"] == "meta":
                    used_prov = chunk["provider"]
                    used_mdl = chunk["model"]
                    yield f"data: {json.dumps({'type': 'meta', 'provider': used_prov, 'model': used_mdl})}\n\n"
                elif chunk["type"] == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': chunk['message']})}\n\n"
                    
            if full_reply.strip():
                cited_str = ",".join(map(str, cited_page_nums)) if cited_page_nums else None
                save_chat_message(session_id, "assistant", full_reply.strip(), cited_str)
                
        except Exception as e:
            logger.error(f"Error in chat stream: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            
    return StreamingResponse(chat_stream_generator(), media_type="text/event-stream")
