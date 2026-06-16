import pytest
import json
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from backend.synthesis import estimate_tokens, synthesise_documents, SynthesisRequest

def test_estimate_tokens():
    text = "one two three four five six"
    # 6 words / 0.75 = 8 tokens
    assert estimate_tokens(text) == 8

@pytest.fixture
def mock_db():
    with patch("backend.synthesis.get_db_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        yield mock_cursor

@pytest.mark.anyio
async def test_synthesise_invalid_document_count():
    # Only 1 document ID
    req = SynthesisRequest(document_ids=[1], question="Compare these")
    with pytest.raises(HTTPException) as exc_info:
        await synthesise_documents(req)
    assert exc_info.value.status_code == 400
    assert "Must provide between 2 and 10" in exc_info.value.detail

@pytest.mark.anyio
async def test_synthesise_empty_question():
    req = SynthesisRequest(document_ids=[1, 2], question="   ")
    with pytest.raises(HTTPException) as exc_info:
        await synthesise_documents(req)
    assert exc_info.value.status_code == 400
    assert "Question cannot be empty" in exc_info.value.detail

@pytest.mark.anyio
async def test_synthesise_unprocessed_documents(mock_db):
    # Mock doc ID 1 exists, but no notes (empty notes select)
    mock_db.fetchone.side_effect = [{"filename": "doc1.pdf"}, {"filename": "doc2.pdf"}]
    mock_db.fetchall.side_effect = [[], []]  # No notes found
    
    req = SynthesisRequest(document_ids=[1, 2], question="What's the difference?")
    
    with pytest.raises(HTTPException) as exc_info:
        await synthesise_documents(req)
    assert exc_info.value.status_code == 400
    assert "Some documents are not processed" in exc_info.value.detail

@pytest.mark.anyio
@patch("backend.main.call_llm")
async def test_synthesise_success_groq_routing(mock_call_llm, mock_db):
    # Mock document exists and has notes
    mock_db.fetchone.side_effect = [{"filename": "doc1.pdf"}, {"filename": "doc2.pdf"}]
    
    note_data_1 = json.dumps({"tldr": "TLDR 1", "key_concepts": [{"term": "C1"}]})
    note_data_2 = json.dumps({"tldr": "TLDR 2", "key_concepts": [{"term": "C2"}]})
    
    mock_db.fetchall.side_effect = [
        [{"page_number": 1, "note_data": note_data_1}],
        [{"page_number": 1, "note_data": note_data_2}]
    ]
    
    # Mock LLM response
    response_json = {
        "answer": "Synthesis answer comparing doc1 and doc2.",
        "citations": [
            {"document_id": 1, "filename": "doc1.pdf", "pages": [1], "excerpt": "TLDR 1"},
            {"document_id": 2, "filename": "doc2.pdf", "pages": [1], "excerpt": "TLDR 2"}
        ]
    }
    mock_call_llm.return_value = (json.dumps(response_json), "groq", "llama-3.3-70b-versatile")
    
    req = SynthesisRequest(document_ids=[1, 2], question="Compare documents")
    res = await synthesise_documents(req)
    
    assert res["answer"] == "Synthesis answer comparing doc1 and doc2."
    assert len(res["citations"]) == 2
    assert res["provider"] == "groq"
    
    # Verify it routed to groq because estimated tokens < 8000
    mock_call_llm.assert_called_once()
    args, kwargs = mock_call_llm.call_args
    assert args[0] == "groq"

@pytest.mark.anyio
@patch("backend.synthesis.estimate_tokens")
@patch("backend.main.call_llm")
async def test_synthesise_success_gemini_routing(mock_call_llm, mock_estimate_tokens, mock_db):
    mock_estimate_tokens.return_value = 9000
    
    # Mock document exists and has notes
    mock_db.fetchone.side_effect = [{"filename": "doc1.pdf"}, {"filename": "doc2.pdf"}]
    
    # Generate long notes
    long_note_data = json.dumps({"tldr": "Long TLDR", "key_concepts": []})
    
    mock_db.fetchall.side_effect = [
        [{"page_number": 1, "note_data": long_note_data}],
        [{"page_number": 1, "note_data": long_note_data}]
    ]
    
    # Mock LLM response
    response_json = {
        "answer": "Synthesis answer comparing doc1 and doc2.",
        "citations": []
    }
    mock_call_llm.return_value = (json.dumps(response_json), "gemini", "gemini-2.0-flash")
    
    req = SynthesisRequest(document_ids=[1, 2], question="Compare documents")
    res = await synthesise_documents(req)
    
    assert res["provider"] == "gemini"
    mock_call_llm.assert_called_once()
    args, kwargs = mock_call_llm.call_args
    assert args[0] == "gemini"
