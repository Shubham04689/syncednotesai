import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import WebSocket
from backend.batch_queue import ConnectionManager, get_jobs, create_job, BatchJobPayload

@pytest.fixture
def mock_db():
    with patch("backend.batch_queue.get_db_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        yield mock_cursor

@pytest.mark.anyio
async def test_connection_manager_connect_disconnect():
    manager = ConnectionManager()
    websocket = AsyncMock(spec=WebSocket)
    
    # Mock get_batch_jobs inside connect
    with patch("backend.batch_queue.get_batch_jobs") as mock_get_jobs:
        mock_get_jobs.return_value = []
        await manager.connect(websocket)
        
        assert websocket in manager.active_connections
        websocket.accept.assert_called_once()
        websocket.send_json.assert_called_once_with({
            "type": "snapshot",
            "jobs": []
        })
        
        manager.disconnect(websocket)
        assert websocket not in manager.active_connections

@pytest.mark.anyio
async def test_connection_manager_broadcast():
    manager = ConnectionManager()
    ws1 = AsyncMock(spec=WebSocket)
    ws2 = AsyncMock(spec=WebSocket)
    manager.active_connections = [ws1, ws2]
    
    msg = {"type": "progress", "value": 0.5}
    await manager.broadcast(msg)
    
    ws1.send_json.assert_called_once_with(msg)
    ws2.send_json.assert_called_once_with(msg)

@pytest.mark.anyio
@patch("backend.batch_queue.get_batch_jobs")
async def test_get_jobs_endpoint(mock_get_jobs):
    mock_get_jobs.return_value = [{"id": 1, "filename": "doc.pdf"}]
    res = await get_jobs()
    assert len(res["jobs"]) == 1
    assert res["jobs"][0]["id"] == 1

@pytest.mark.anyio
@patch("backend.batch_queue.save_batch_job")
@patch("backend.batch_queue.batch_worker")
async def test_create_job_endpoint(mock_worker, mock_save_job):
    mock_save_job.return_value = 42
    mock_worker.add_job = AsyncMock()
    
    payload = BatchJobPayload(
        filename="test.pdf",
        file_path="/path/to/test.pdf",
        provider="gemini",
        model="gemini-2.0-flash"
    )
    
    res = await create_job(payload)
    assert res["job_id"] == 42
    mock_save_job.assert_called_once_with(
        filename="test.pdf",
        file_path="/path/to/test.pdf",
        model="gemini-2.0-flash",
        provider="gemini"
    )
    mock_worker.add_job.assert_called_once_with(42)
