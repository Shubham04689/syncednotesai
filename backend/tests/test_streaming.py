import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request
from backend.streaming import parse_partial_note_data, generate_page_note_stream_sse, clean_json_response

class MockGenerateRequest:
    def __init__(self, document_id, page_number, text, model, provider):
        self.document_id = document_id
        self.page_number = page_number
        self.text = text
        self.model = model
        self.provider = provider

def test_parse_partial_note_data():
    # Test title parsing
    buffer = '{"notes": {"title": "Introduction to AI", "summary": "AI is...'
    parsed = parse_partial_note_data(buffer)
    assert parsed.get("title") == "Introduction to AI"
    assert "summary" not in parsed

    # Test summary parsing
    buffer = '{"notes": {"title": "Introduction to AI", "summary": "AI is a field."}}'
    parsed = parse_partial_note_data(buffer)
    assert parsed.get("title") == "Introduction to AI"
    assert parsed.get("summary") == "AI is a field."

    # Test key concepts parsing
    buffer = """
    {"notes": {
        "title": "Intro",
        "key_concepts": [
            {"term": "ML", "definition": "Machine Learning", "example": "regression"}
        ]
    }}
    """
    parsed = parse_partial_note_data(buffer)
    assert len(parsed.get("key_concepts", [])) == 1
    assert parsed["key_concepts"][0]["term"] == "ML"

    # Test sections parsing
    buffer = """
    {"notes": {
        "sections": [
            {
                "heading": "Heading 1",
                "points": ["point 1", "point 2"],
                "code_blocks": []
            }
        ]
    }}
    """
    parsed = parse_partial_note_data(buffer)
    assert len(parsed.get("sections", [])) == 1
    assert parsed["sections"][0]["heading"] == "Heading 1"
    assert parsed["sections"][0]["points"] == ["point 1", "point 2"]

@pytest.mark.anyio
@patch("backend.streaming.get_note")
async def test_streaming_cache_hit(mock_get_note):
    # Setup mock note cache
    mock_get_note.return_value = {
        "provider": "groq",
        "model": "llama3",
        "note_data": json.dumps({
            "title": "Cached Title",
            "summary": "Cached Summary",
            "sections": []
        })
    }

    req = MockGenerateRequest(1, 1, "test text", "llama3", "groq")
    mock_request = AsyncMock(spec=Request)
    mock_request.is_disconnected = AsyncMock(return_value=False)

    generator = generate_page_note_stream_sse(req, mock_request)
    events = []
    async for event in generator:
        events.append(event)

    assert len(events) == 2
    assert "Cached Title" in events[0]
    assert "[DONE]" in events[1]
