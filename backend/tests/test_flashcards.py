import json
import pytest
from unittest.mock import patch, MagicMock
from backend.flashcards import build_apkg_bytes, router

def test_sm2_algorithm_quality_less_than_3():
    # quality < 3 resets sm2_n and interval to 1
    # Test quality = 1
    req = {"card_id": 1, "quality": 1}
    
    # We patch database functions and get_db_connection
    with patch("backend.flashcards.get_db_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        
        # Mock SELECT query returning a card
        mock_cursor.fetchone.return_value = {
            "id": 1,
            "sm2_n": 3,
            "sm2_easiness": 2.5,
            "sm2_interval": 10,
            "sm2_next_review": "2026-06-15"
        }
        
        with patch("backend.flashcards.update_flashcard_sm2") as mock_update:
            # We mock call to review_flashcard endpoint directly
            from backend.flashcards import review_flashcard
            result = asyncio_run(review_flashcard(req))
            
            assert result["sm2_n"] == 0
            assert result["sm2_interval"] == 1
            assert result["sm2_easiness"] == 2.5  # easiness remains unchanged
            mock_update.assert_called_once()

def test_sm2_algorithm_quality_geq_3():
    # quality >= 3 updates sm2_n and interval
    # First correct review: interval = 1
    req = {"card_id": 1, "quality": 4}
    
    with patch("backend.flashcards.get_db_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = {
            "id": 1,
            "sm2_n": 0,
            "sm2_easiness": 2.5,
            "sm2_interval": 1,
            "sm2_next_review": "2026-06-15"
        }
        
        with patch("backend.flashcards.update_flashcard_sm2") as mock_update:
            from backend.flashcards import review_flashcard
            result = asyncio_run(review_flashcard(req))
            
            assert result["sm2_n"] == 1
            assert result["sm2_interval"] == 1
            assert result["sm2_easiness"] > 1.3
            
    # Second correct review: interval = 6
    with patch("backend.flashcards.get_db_connection") as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = {
            "id": 1,
            "sm2_n": 1,
            "sm2_easiness": 2.5,
            "sm2_interval": 1,
            "sm2_next_review": "2026-06-15"
        }
        
        with patch("backend.flashcards.update_flashcard_sm2") as mock_update:
            from backend.flashcards import review_flashcard
            result = asyncio_run(review_flashcard(req))
            
            assert result["sm2_n"] == 2
            assert result["sm2_interval"] == 6

def test_build_apkg_bytes():
    cards = [
        {"front": "Q1", "back": "A1", "difficulty": "basic"},
        {"front": "Q2", "back": "A2", "difficulty": "intermediate"}
    ]
    apkg_data = build_apkg_bytes(cards, "Test Deck")
    assert isinstance(apkg_data, bytes)
    assert len(apkg_data) > 0
    # Must start with zip file header 'PK\x03\x04'
    assert apkg_data.startswith(b"PK\x03\x04")

def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)
