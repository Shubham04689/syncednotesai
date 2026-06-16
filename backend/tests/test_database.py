import os
import pytest
from pathlib import Path
from backend.database import (
    DB_PATH,
    init_db,
    save_document,
    get_document_by_hash,
    save_note,
    get_note
)

@pytest.fixture(autouse=True)
def setup_teardown_db():
    # If DB exists, delete it for a clean test run
    if DB_PATH.exists():
        try:
            os.remove(str(DB_PATH))
        except OSError:
            pass
    init_db()
    yield
    # Clean up after test
    if DB_PATH.exists():
        try:
            os.remove(str(DB_PATH))
        except OSError:
            pass

def test_document_caching_lifecycle():
    doc_hash = "mock_sha256_hash_value"
    filename = "test_document.pdf"
    pages = [
        {"page_number": 1, "text": "This is page 1 text content."},
        {"page_number": 2, "text": "This is page 2 text content."}
    ]

    # Verify no document matches initially
    assert get_document_by_hash(doc_hash) is None

    # Save document
    doc_id = save_document(doc_hash, filename, pages)
    assert doc_id > 0

    # Retrieve and verify cached document matches
    cached = get_document_by_hash(doc_hash)
    assert cached is not None
    assert cached["id"] == doc_id
    assert cached["filename"] == filename
    assert len(cached["pages"]) == 2
    assert cached["pages"][0]["page_number"] == 1
    assert cached["pages"][0]["text"] == "This is page 1 text content."

def test_note_caching_lifecycle():
    doc_hash = "mock_hash_for_notes"
    pages = [{"page_number": 1, "text": "Content for notes test"}]
    doc_id = save_document(doc_hash, "doc.pdf", pages)

    # Verify no notes exist initially
    assert get_note(doc_id, 1) is None

    # Save note cache
    model = "gemini-1.5-flash"
    provider = "gemini"
    note_data = '{"title": "Test Title", "summary": "Test Summary"}'
    mind_map = "graph TD\n  A --> B"
    infographic = "<svg>Test SVG</svg>"

    save_note(
        doc_id=doc_id,
        page_number=1,
        model=model,
        provider=provider,
        note_data=note_data,
        mind_map=mind_map,
        infographic=infographic
    )

    # Retrieve and verify note cache matches
    cached_note = get_note(doc_id, 1)
    assert cached_note is not None
    assert cached_note["model"] == model
    assert cached_note["provider"] == provider
    assert cached_note["note_data"] == note_data
    assert cached_note["mind_map"] == mind_map
    assert cached_note["infographic"] == infographic
