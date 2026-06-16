import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from backend.chat import get_or_create_session, get_history, send_chat_message, ChatMessageRequest

@pytest.fixture
def mock_db():
    with patch("backend.chat.get_db_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        yield mock_cursor

@pytest.mark.anyio
@patch("backend.chat.get_chat_session_by_doc")
@patch("backend.chat.create_chat_session")
async def test_get_or_create_session_existing(mock_create, mock_get):
    mock_get.return_value = 42
    res = await get_or_create_session(doc_id=1)
    assert res["session_id"] == 42
    mock_get.assert_called_once_with(1)
    mock_create.assert_not_called()

@pytest.mark.anyio
@patch("backend.chat.get_chat_session_by_doc")
@patch("backend.chat.create_chat_session")
async def test_get_or_create_session_new(mock_create, mock_get):
    mock_get.return_value = None
    mock_create.return_value = 100
    res = await get_or_create_session(doc_id=1)
    assert res["session_id"] == 100
    mock_get.assert_called_once_with(1)
    mock_create.assert_called_once_with(1)

@pytest.mark.anyio
@patch("backend.chat.get_chat_history")
async def test_get_history(mock_get_history):
    mock_get_history.return_value = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"}
    ]
    res = await get_history(session_id=42)
    assert len(res["messages"]) == 2
    assert res["messages"][0]["role"] == "user"
    mock_get_history.assert_called_once_with(42)

@pytest.mark.anyio
@patch("backend.chat.ensure_page_embeddings")
@patch("backend.chat.find_top_k_pages")
@patch("backend.chat.save_chat_message")
@patch("backend.chat.get_chat_history")
@patch("backend.chat.generate_llm_stream")
async def test_send_chat_message_success(
    mock_gen_stream, mock_history, mock_save, mock_find_pages, mock_ensure, mock_db
):
    # Mock database session lookup
    mock_db.fetchone.return_value = {"document_id": 1}
    mock_db.fetchall.return_value = [{"page_number": 1, "text": "Page 1 content"}]
    
    # Mock embeddings and search
    mock_ensure.return_value = True
    mock_find_pages.return_value = [
        {"page_number": 1, "text": "Page 1 content", "similarity": 0.8}
    ]
    
    # Mock chat history
    mock_history.return_value = []
    
    # Mock LLM stream chunks
    async def mock_stream(*args, **kwargs):
        yield {"type": "meta", "provider": "groq", "model": "llama3"}
        yield {"type": "token", "text": "Answer from AI"}
    mock_gen_stream.side_effect = mock_stream
    
    req = ChatMessageRequest(session_id=42, message="What is AI?")
    
    # Mock Request client disconnection check
    mock_request = AsyncMock()
    mock_request.is_disconnected.return_value = False
    
    response = await send_chat_message(req, mock_request)
    assert response is not None
    
    # Read stream chunks
    chunks = []
    async for chunk in response.body_iterator:
        val = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        chunks.append(val)
        
    assert len(chunks) > 0
    # The first event is the citation event
    assert "citations" in chunks[0]
    # The second is metadata
    assert "meta" in chunks[1]
    # The third is token
    assert "token" in chunks[2]
    
    # Verify mock calls
    mock_ensure.assert_called_once()
    mock_find_pages.assert_called_once()
    mock_save.assert_any_call(42, "user", "What is AI?")
    mock_save.assert_any_call(42, "assistant", "Answer from AI", "1")
