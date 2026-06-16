import json
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging

try:
    from backend.database import (
        get_db_connection, save_document_intelligence, get_document_intelligence,
        get_document_by_id
    )
    from backend.streaming import clean_json_response
except ImportError:
    from database import (
        get_db_connection, save_document_intelligence, get_document_intelligence,
        get_document_by_id
    )
    from streaming import clean_json_response

logger = logging.getLogger("SyncedNotesAI.DocIntel")
router = APIRouter(prefix="/api/document-intelligence")

class DocIntelRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None

@router.get("/{doc_id}")
async def fetch_document_intelligence(doc_id: int):
    """Retrieves document intelligence if cached."""
    cached = get_document_intelligence(doc_id)
    if cached:
        # Deserialize fields
        try:
            concept_index = json.loads(cached["concept_index"])
        except Exception:
            concept_index = []
            
        try:
            chapter_groups = json.loads(cached["chapter_groups"])
        except Exception:
            chapter_groups = []
            
        try:
            prerequisite_knowledge = json.loads(cached["prerequisite_knowledge"])
        except Exception:
            prerequisite_knowledge = []
            
        return {
            "cached": True,
            "executive_summary": cached["executive_summary"],
            "concept_index": concept_index,
            "chapter_groups": chapter_groups,
            "difficulty_score": cached["difficulty_score"],
            "prerequisite_knowledge": prerequisite_knowledge,
            "_meta": {
                "provider": cached["provider"],
                "model": cached["model"]
            }
        }
    return {"cached": False}

@router.post("/{doc_id}")
async def generate_document_intelligence(doc_id: int, req: DocIntelRequest):
    try:
        from backend.main import call_llm
    except ImportError:
        from main import call_llm

    # 1. Check cache first
    cached = get_document_intelligence(doc_id)
    if cached:
        try:
            concept_index = json.loads(cached["concept_index"])
        except Exception:
            concept_index = []
        try:
            chapter_groups = json.loads(cached["chapter_groups"])
        except Exception:
            chapter_groups = []
        try:
            prerequisite_knowledge = json.loads(cached["prerequisite_knowledge"])
        except Exception:
            prerequisite_knowledge = []
            
        return {
            "cached": True,
            "executive_summary": cached["executive_summary"],
            "concept_index": concept_index,
            "chapter_groups": chapter_groups,
            "difficulty_score": cached["difficulty_score"],
            "prerequisite_knowledge": prerequisite_knowledge,
            "_meta": {
                "provider": cached["provider"],
                "model": cached["model"]
            }
        }
        
    # 2. Check completeness gate
    doc = get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
        
    num_pages = len(doc["pages"])
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT page_number, note_data FROM notes WHERE document_id = ?", (doc_id,))
        rows = cursor.fetchall()
    finally:
        conn.close()
        
    notes_count = len(rows)
    if notes_count < num_pages:
        raise HTTPException(
            status_code=400,
            detail=f"Document is not fully processed. {num_pages - notes_count} pages remaining out of {num_pages}."
        )
        
    # 3. Extract TLDR and Key Concepts from notes
    page_data_list = []
    for r in sorted(rows, key=lambda x: x["page_number"]):
        pnum = r["page_number"]
        try:
            note_obj = json.loads(r["note_data"])
            tldr = note_obj.get("tldr", "")
            concepts = [c.get("term", "") for c in note_obj.get("key_concepts", [])]
            page_data_list.append({
                "page": pnum,
                "tldr": tldr[:200],  # safety cap
                "concepts": concepts
            })
        except Exception as e:
            logger.warning(f"Error parsing note data for page {pnum}: {e}")
            
    # Default provider and model
    provider = req.provider or "gemini"
    model = req.model
    if not model:
        model = "gemini-2.0-flash" if provider == "gemini" else "llama-3.3-70b-versatile"
        
    # 4. Map-Reduce / Summary Aggregation
    summaries_text_blocks = []
    
    if num_pages > 60:
        # Chunk into groups of 20
        chunk_size = 20
        for i in range(0, len(page_data_list), chunk_size):
            chunk = page_data_list[i : i + chunk_size]
            start_p = chunk[0]["page"]
            end_p = chunk[-1]["page"]
            
            chunk_input_parts = []
            for p in chunk:
                chunk_input_parts.append(
                    f"Page {p['page']}: TLDR: {p['tldr']}. Concepts: {', '.join(p['concepts'])}"
                )
            chunk_input = "\n".join(chunk_input_parts)
            
            chunk_prompt = (
                f"Summarize the following page-by-page information for pages {start_p} to {end_p} "
                "into a cohesive, structured section summary (at most 2 paragraphs):\n\n"
                f"{chunk_input}"
            )
            
            raw_chunk_summary, _, _ = await call_llm(provider, model, chunk_prompt, expect_json=False)
            summaries_text_blocks.append(
                f"Section (Pages {start_p}-{end_p}):\n{raw_chunk_summary.strip()}"
            )
    else:
        # Single pass
        for p in page_data_list:
            summaries_text_blocks.append(
                f"Page {p['page']}: TLDR: {p['tldr']}. Concepts: {', '.join(p['concepts'])}"
            )
            
    aggregation_input = "\n\n".join(summaries_text_blocks)
    
    # 5. Final Aggregation Prompt
    final_prompt = (
        "You are an expert document analysis AI.\n"
        "We have parsed a document page-by-page. Below are the page summaries and key concepts for each page/section.\n"
        "Analyze the entire document and produce a comprehensive document intelligence report in JSON format.\n\n"
        f"Document Content Summary:\n{aggregation_input}\n\n"
        "Your response MUST be a valid JSON object matching this schema:\n"
        "{\n"
        "  \"executive_summary\": \"A detailed 3 to 5 paragraph summary of the entire document.\",\n"
        "  \"concept_index\": [\n"
        "    {\"term\": \"concept name\", \"definition\": \"clear definition based on context\", \"pages\": [1, 2]}\n"
        "  ],\n"
        "  \"chapter_groups\": [\n"
        "    {\"title\": \"chapter or topic title\", \"page_start\": 1, \"page_end\": 10, \"summary\": \"summary of this chapter/section\"}\n"
        "  ],\n"
        "  \"difficulty_score\": 3,  // Integer between 1 (introductory) and 5 (advanced)\n"
        "  \"prerequisite_knowledge\": [\"required concept 1\", \"required concept 2\"]\n"
        "}\n"
        "Do NOT return any explanation, code fences, or text outside of the JSON object."
    )
    
    raw_final, used_provider, used_model = await call_llm(provider, model, final_prompt, expect_json=True)
    cleaned_json = clean_json_response(raw_final)
    
    try:
        data = json.loads(cleaned_json)
    except Exception as e:
        logger.error(f"Failed to parse final doc intel JSON: {e}. Raw response: {raw_final}")
        # Create a basic fallback structure
        data = {
            "executive_summary": "Failed to generate summary. " + raw_final[:400],
            "concept_index": [],
            "chapter_groups": [],
            "difficulty_score": 3,
            "prerequisite_knowledge": []
        }
        
    # Save to database
    save_document_intelligence(
        doc_id=doc_id,
        summary=data.get("executive_summary", ""),
        concepts=json.dumps(data.get("concept_index", [])),
        chapters=json.dumps(data.get("chapter_groups", [])),
        diff_score=int(data.get("difficulty_score", 3)),
        prerequisites=json.dumps(data.get("prerequisite_knowledge", [])),
        model=used_model,
        provider=used_provider
    )
    
    return {
        "cached": False,
        "executive_summary": data.get("executive_summary", ""),
        "concept_index": data.get("concept_index", []),
        "chapter_groups": data.get("chapter_groups", []),
        "difficulty_score": data.get("difficulty_score", 3),
        "prerequisite_knowledge": data.get("prerequisite_knowledge", []),
        "_meta": {
            "provider": used_provider,
            "model": used_model
        }
    }
