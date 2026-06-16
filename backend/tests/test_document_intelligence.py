import pytest
import json
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from backend.document_intelligence import fetch_document_intelligence, generate_document_intelligence, DocIntelRequest

@pytest.fixture
def mock_db():
    with patch("backend.document_intelligence.get_db_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        yield mock_cursor

@pytest.mark.anyio
@patch("backend.document_intelligence.get_document_intelligence")
async def test_fetch_document_intelligence_cached(mock_get_intel):
    mock_get_intel.return_value = {
        "executive_summary": "Summary text",
        "concept_index": json.dumps([{"term": "C1"}]),
        "chapter_groups": json.dumps([]),
        "difficulty_score": 3,
        "prerequisite_knowledge": json.dumps([]),
        "provider": "gemini",
        "model": "gemini-2.0-flash"
    }
    
    res = await fetch_document_intelligence(doc_id=1)
    assert res["cached"] is True
    assert res["executive_summary"] == "Summary text"
    assert res["concept_index"] == [{"term": "C1"}]
    mock_get_intel.assert_called_once_with(1)

@pytest.mark.anyio
@patch("backend.document_intelligence.get_document_intelligence")
async def test_fetch_document_intelligence_not_cached(mock_get_intel):
    mock_get_intel.return_value = None
    res = await fetch_document_intelligence(doc_id=1)
    assert res["cached"] is False

@pytest.mark.anyio
@patch("backend.document_intelligence.get_document_intelligence")
@patch("backend.document_intelligence.get_document_by_id")
async def test_generate_document_intelligence_gate_fails(mock_get_doc, mock_get_intel, mock_db):
    mock_get_intel.return_value = None
    # Document has 3 pages
    mock_get_doc.return_value = {
        "id": 1,
        "filename": "doc.pdf",
        "pages": [{"page_number": 1}, {"page_number": 2}, {"page_number": 3}]
    }
    
    # Only 2 pages are processed in notes table
    mock_db.fetchall.return_value = [
        {"page_number": 1, "note_data": "{}"},
        {"page_number": 2, "note_data": "{}"}
    ]
    
    req = DocIntelRequest(provider="gemini")
    with pytest.raises(HTTPException) as exc_info:
        await generate_document_intelligence(doc_id=1, req=req)
        
    assert exc_info.value.status_code == 400
    assert "is not fully processed" in exc_info.value.detail

@pytest.mark.anyio
@patch("backend.document_intelligence.get_document_intelligence")
@patch("backend.document_intelligence.get_document_by_id")
@patch("backend.main.call_llm")
@patch("backend.document_intelligence.save_document_intelligence")
async def test_generate_document_intelligence_single_pass_success(
    mock_save, mock_call_llm, mock_get_doc, mock_get_intel, mock_db
):
    mock_get_intel.return_value = None
    
    # Document has 2 pages
    mock_get_doc.return_value = {
        "id": 1,
        "filename": "doc.pdf",
        "pages": [{"page_number": 1}, {"page_number": 2}]
    }
    
    # Notes are complete
    note_data = json.dumps({"tldr": "page tldr", "key_concepts": [{"term": "C1"}]})
    mock_db.fetchall.return_value = [
        {"page_number": 1, "note_data": note_data},
        {"page_number": 2, "note_data": note_data}
    ]
    
    # Mock LLM response for final report
    intel_report = {
        "executive_summary": "Full summary prose",
        "concept_index": [{"term": "C1", "definition": "Def 1", "pages": [1]}],
        "chapter_groups": [{"title": "Ch 1", "page_start": 1, "page_end": 2, "summary": "Ch summary"}],
        "difficulty_score": 4,
        "prerequisite_knowledge": ["P1"]
    }
    mock_call_llm.return_value = (json.dumps(intel_report), "gemini", "gemini-2.0-flash")
    
    req = DocIntelRequest(provider="gemini")
    res = await generate_document_intelligence(doc_id=1, req=req)
    
    assert res["cached"] is False
    assert res["executive_summary"] == "Full summary prose"
    assert res["difficulty_score"] == 4
    
    # Assert save was called
    mock_save.assert_called_once()
    assert mock_call_llm.call_count == 1  # Only 1 LLM call since num_pages <= 60
