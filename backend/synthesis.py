import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging

try:
    from backend.database import get_db_connection, get_document_by_id
    from backend.streaming import clean_json_response
except ImportError:
    from database import get_db_connection, get_document_by_id
    from streaming import clean_json_response

logger = logging.getLogger("SyncedNotesAI.Synthesis")
router = APIRouter(prefix="/api")

class SynthesisRequest(BaseModel):
    document_ids: List[int]
    question: str

def estimate_tokens(text: str) -> int:
    return int(len(text.split()) / 0.75)

@router.post("/synthesise")
async def synthesise_documents(req: SynthesisRequest):
    try:
        from backend.main import call_llm
    except ImportError:
        from main import call_llm
    """
    Synthesizes answers to a question spanning 2-10 documents.
    Gathers TLDRs and key concepts, calculates tokens to route to Gemini (>8k) or Groq (<=8k),
    and returns a structured JSON answer with source citations.
    """
    doc_ids = req.document_ids
    question = req.question.strip()
    
    if not (2 <= len(doc_ids) <= 10):
        raise HTTPException(status_code=400, detail="Must provide between 2 and 10 document IDs for synthesis.")
        
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
        
    conn = get_db_connection()
    unprocessed_filenames = []
    documents_data = {}
    
    try:
        cursor = conn.cursor()
        for doc_id in doc_ids:
            # Get document info
            cursor.execute("SELECT filename FROM documents WHERE id = ?", (doc_id,))
            doc_row = cursor.fetchone()
            if not doc_row:
                raise HTTPException(status_code=404, detail=f"Document ID {doc_id} not found.")
            filename = doc_row["filename"]
            
            # Get cached page notes
            cursor.execute("SELECT page_number, note_data FROM notes WHERE document_id = ?", (doc_id,))
            note_rows = cursor.fetchall()
            if not note_rows:
                unprocessed_filenames.append(filename)
                continue
                
            pages_extracted = []
            for nr in note_rows:
                pnum = nr["page_number"]
                try:
                    note_obj = json.loads(nr["note_data"])
                    tldr = note_obj.get("tldr", "")
                    concepts = [c.get("term", "") for c in note_obj.get("key_concepts", [])[:10]]
                    pages_extracted.append({
                        "page_number": pnum,
                        "tldr": tldr[:150],  # safety cap
                        "concepts": concepts
                    })
                except Exception as e:
                    logger.warning(f"Error extracting note details for doc {doc_id} page {pnum}: {e}")
                    
            documents_data[doc_id] = {
                "filename": filename,
                "pages": sorted(pages_extracted, key=lambda x: x["page_number"])
            }
    finally:
        conn.close()
        
    if unprocessed_filenames:
        raise HTTPException(
            status_code=400,
            detail=f"Some documents are not processed: {', '.join(unprocessed_filenames)}"
        )
        
    # Build prompt content
    doc_blocks = []
    for doc_id, doc_info in documents_data.items():
        doc_block_parts = [f"Document ID {doc_id} - Filename: {doc_info['filename']}"]
        for p in doc_info["pages"]:
            doc_block_parts.append(
                f"  Page {p['page_number']}:\n"
                f"    TLDR: {p['tldr']}\n"
                f"    Concepts: {', '.join(p['concepts'])}"
            )
        doc_blocks.append("\n".join(doc_block_parts))
        
    doc_data_str = "\n\n===\n\n".join(doc_blocks)
    
    final_prompt = (
        "You are an expert academic synthesiser.\n"
        "Answer the user's research question using the document information provided below.\n\n"
        "Document Data:\n"
        f"{doc_data_str}\n\n"
        f"Question: {question}\n\n"
        "Instructions:\n"
        "- Base your answer strictly on the provided document data.\n"
        "- Synthesise a cohesive, structured prose answer.\n"
        "- You MUST use the citation format `[Doc N, Page M]` where N is the Document ID and M is the Page Number (e.g. [Doc 1, Page 4]) when referencing specific facts in your answer.\n"
        "- Provide a list of citations used.\n\n"
        "Your response MUST be a valid JSON object matching this schema:\n"
        "{\n"
        "  \"answer\": \"Your detailed synthesised prose answer...\",\n"
        "  \"citations\": [\n"
        "    {\n"
        "      \"document_id\": 1,\n"
        "      \"filename\": \"filename.pdf\",\n"
        "      \"pages\": [3, 4],\n"
        "      \"excerpt\": \"a brief quoted phrase from that document illustrating the fact\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "Do NOT return any explanation, code fences, or text outside of the JSON object."
    )
    
    # Route provider: > 8000 tokens -> Gemini, otherwise Groq
    tokens = estimate_tokens(final_prompt)
    if tokens > 8000:
        provider = "gemini"
        model = "gemini-2.0-flash"
    else:
        provider = "groq"
        model = "llama-3.3-70b-versatile"
        
    try:
        raw_res, used_provider, used_model = await call_llm(provider, model, final_prompt, expect_json=True)
        cleaned_json = clean_json_response(raw_res)
        data = json.loads(cleaned_json)
        
        return {
            "answer": data.get("answer", ""),
            "citations": data.get("citations", []),
            "provider": used_provider,
            "model": used_model
        }
    except Exception as e:
        logger.error(f"Error in multi-document synthesis: {e}")
        raise HTTPException(status_code=500, detail=str(e))
