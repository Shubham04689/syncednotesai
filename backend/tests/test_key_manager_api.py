import pytest
import json
from unittest.mock import AsyncMock, patch, mock_open
from fastapi import HTTPException
from backend.key_manager_api import mask_key, get_keys, save_keys, probe_key, KeysPayload, ProbeRequest

def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("short") == "****"
    assert mask_key("AI_KEY_12345678") == "AI_K...5678"

@pytest.mark.anyio
@patch("backend.key_manager_api.settings")
async def test_get_keys(mock_settings):
    mock_settings.gemini_keys = ["gemini_secret_1", "gemini_secret_2"]
    mock_settings.groq_keys = ["groq_secret_key"]
    mock_settings.mistral_keys = []
    
    res = await get_keys()
    assert len(res["gemini"]) == 2
    assert res["gemini"][0] == "gemi...et_1"
    assert res["groq"][0] == "groq..._key"
    assert res["mistral"] == []

@pytest.mark.anyio
@patch("builtins.open", new_callable=mock_open)
@patch("backend.key_manager_api.settings")
@patch("backend.key_manager_api.key_manager")
async def test_save_keys(mock_key_manager, mock_settings, mock_file):
    payload = KeysPayload(
        gemini=["new_gemini_key"],
        groq=["new_groq_key"],
        mistral=[]
    )
    
    res = await save_keys(payload)
    assert res["status"] == "success"
    
    # Assert keys.json was written
    mock_file.assert_called_once()
    handle = mock_file()
    written_data = "".join(call.args[0] for call in handle.write.call_args_list)
    parsed_written_data = json.loads(written_data)
    assert parsed_written_data["gemini"] == ["new_gemini_key"]
    assert parsed_written_data["groq"] == ["new_groq_key"]
    assert parsed_written_data["mistral"] == []
    
    # Assert reload and rebuild were triggered
    mock_settings.reload_from_keys_json.assert_called_once()
    assert mock_key_manager.rebuild_provider.call_count == 3

@pytest.mark.anyio
@patch("backend.main.fetch_gemini_models")
async def test_probe_key_gemini_success(mock_fetch_models):
    mock_fetch_models.return_value = ["gemini-2.0-flash", "gemini-1.5-pro"]
    
    req = ProbeRequest(provider="gemini", key="test_gemini_key")
    res = await probe_key(req)
    assert res["valid"] is True
    assert "Successfully validated" in res["message"]
    mock_fetch_models.assert_called_once_with("test_gemini_key")

@pytest.mark.anyio
@patch("backend.main.fetch_groq_models")
async def test_probe_key_groq_failure(mock_fetch_models):
    mock_fetch_models.return_value = []
    
    req = ProbeRequest(provider="groq", key="test_groq_key")
    res = await probe_key(req)
    assert res["valid"] is False
    assert "failed" in res["message"]
    mock_fetch_models.assert_called_once_with("test_groq_key")

@pytest.mark.anyio
async def test_probe_key_invalid_provider():
    req = ProbeRequest(provider="unsupported", key="some_key")
    with pytest.raises(HTTPException) as exc_info:
        await probe_key(req)
    assert exc_info.value.status_code == 400
    assert "Unsupported provider" in exc_info.value.detail
