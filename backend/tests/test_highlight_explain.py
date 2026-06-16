import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from backend.highlight_explain import estimate_tokens, ExplainRequest, explain_text

def test_estimate_tokens():
    text = "Hello world from SyncedNotes AI assistant"
    # 6 words, so 6 / 0.75 = 8 tokens
    assert estimate_tokens(text) == 8

@pytest.mark.anyio
@patch("backend.main.call_llm")
async def test_explain_text_explain_action(mock_call_llm):
    mock_call_llm.return_value = ("AI is machines acting like humans.", "groq", "llama-3.3-70b-versatile")
    
    req = ExplainRequest(
        text="Artificial Intelligence",
        action="explain",
        provider="groq"
    )
    
    res = await explain_text(req)
    assert res["explanation"] == "AI is machines acting like humans."
    assert res["action"] == "explain"
    assert res["_meta"]["provider"] == "groq"
    assert res["_meta"]["model"] == "llama-3.3-70b-versatile"
    mock_call_llm.assert_called_once()

@pytest.mark.anyio
@patch("backend.main.call_llm")
async def test_explain_text_define_action_with_context(mock_call_llm):
    mock_call_llm.return_value = ("To split text into smaller units.", "gemini", "gemini-2.0-flash")
    
    req = ExplainRequest(
        text="tokenize",
        action="define",
        context="We will tokenize the text string to compute term frequencies.",
        provider="gemini"
    )
    
    res = await explain_text(req)
    assert res["explanation"] == "To split text into smaller units."
    assert res["action"] == "define"
    assert res["_meta"]["provider"] == "gemini"
    
    # Check that context was included in the call to call_llm (the prompt argument)
    args, kwargs = mock_call_llm.call_args
    prompt = kwargs.get("prompt") or args[2]
    assert "tokenize" in prompt
    assert "term frequencies" in prompt

@pytest.mark.anyio
async def test_explain_text_invalid_action():
    req = ExplainRequest(
        text="some text",
        action="invalid_action"
    )
    with pytest.raises(HTTPException) as exc_info:
        await explain_text(req)
    assert exc_info.value.status_code == 400
    assert "Invalid action" in exc_info.value.detail

@pytest.mark.anyio
async def test_explain_text_empty_text():
    req = ExplainRequest(
        text="   ",
        action="explain"
    )
    with pytest.raises(HTTPException) as exc_info:
        await explain_text(req)
    assert exc_info.value.status_code == 400
    assert "Selected text cannot be empty" in exc_info.value.detail

@pytest.mark.anyio
@patch("backend.main.call_llm")
async def test_explain_text_truncation(mock_call_llm):
    mock_call_llm.return_value = ("Simplified text", "groq", "llama-3.3-70b-versatile")
    
    # Generate very long text > 500 chars
    long_text = "word " * 120
    req = ExplainRequest(
        text=long_text,
        action="simplify"
    )
    
    await explain_text(req)
    args, kwargs = mock_call_llm.call_args
    prompt = kwargs.get("prompt") or args[2]
    
    # verify length is within limits
    assert len(prompt) < 1000
    assert "..." in prompt
