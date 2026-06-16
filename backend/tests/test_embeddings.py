import pytest
from backend.embeddings import (
    tokenize,
    compute_tfidf_vocab_and_idf,
    compute_tfidf_vector,
    cosine_similarity,
    find_top_k_pages,
    ensure_page_embeddings
)
from backend.database import init_db, save_document, DB_PATH
import os

@pytest.fixture(autouse=True)
def setup_teardown_db():
    if DB_PATH.exists():
        try:
            os.remove(str(DB_PATH))
        except OSError:
            pass
    init_db()
    yield
    if DB_PATH.exists():
        try:
            os.remove(str(DB_PATH))
        except OSError:
            pass

def test_tokenization_and_tfidf():
    text = "The quick brown fox jumps over the lazy dog!"
    tokens = tokenize(text)
    assert "quick" in tokens
    assert "fox" in tokens
    assert "the" in tokens
    assert len(tokens) > 5

    # Test IDF calculation
    pages = [
        "Python programming language",
        "Learning Python for coding",
        "FastAPI is a Python web framework"
    ]
    idf = compute_tfidf_vocab_and_idf(pages)
    # "python" appears in all 3 pages, so its document frequency is 3.
    # IDF of "python" = log(1 + 3 / (1 + 3)) = log(1 + 0.75) = log(1.75) ≈ 0.559
    # "fastapi" appears in 1 page, so df is 1.
    # IDF of "fastapi" = log(1 + 3 / (1 + 1)) = log(1 + 1.5) = log(2.5) ≈ 0.916
    assert idf["python"] < idf["fastapi"]

    # Test vector calculation
    vec = compute_tfidf_vector("Python FastAPI", idf)
    assert "python" in vec
    assert "fastapi" in vec
    assert vec["fastapi"] > vec["python"]

def test_cosine_similarity():
    vec_a = {"python": 0.8, "coding": 0.2}
    vec_b = {"python": 0.9, "programming": 0.3}
    sim = cosine_similarity(vec_a, vec_b)
    assert 0.7 < sim < 1.0

    # Orthogonal vectors
    vec_c = {"fastapi": 1.0}
    assert cosine_similarity(vec_a, vec_c) == 0.0

    # Lists cosine similarity
    list_a = [1.0, 0.0, 0.0]
    list_b = [0.8, 0.6, 0.0]
    assert cosine_similarity(list_a, list_b) == pytest.approx(0.8)

@pytest.mark.anyio
async def test_find_top_k_pages():
    pages = [
        {"page_number": 1, "text": "Linear algebra concerns vector spaces and linear mappings."},
        {"page_number": 2, "text": "Calculus is the mathematical study of continuous change."},
        {"page_number": 3, "text": "Statistics is the discipline that concerns the collection, organization, analysis, interpretation, and presentation of data."}
    ]
    doc_id = save_document("test_rag_hash", "rag_doc.pdf", pages)
    
    # Run ensure embeddings
    success = await ensure_page_embeddings(doc_id, pages)
    assert success is True

    # Search for mathematical change
    matches = await find_top_k_pages(doc_id, "continuous change calculus", k=1)
    assert len(matches) == 1
    assert matches[0]["page_number"] == 2
