import json
import math
import re
import httpx
from typing import List, Dict, Any, Union, Optional
import logging

try:
    from backend.config import settings
    from backend.database import get_db_connection
except ImportError:
    from config import settings
    from database import get_db_connection

logger = logging.getLogger("SyncedNotesAI.Embeddings")

# ── Pure Python TF-IDF Embeddings ──────────────────────────────────────────────
def tokenize(text: str) -> List[str]:
    """Helper to lowercase, strip punctuation, and split text into tokens."""
    text = text.lower()
    # Replace non-alphanumeric characters with spaces
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return [w for w in text.split() if len(w) > 1]

def compute_tfidf_vocab_and_idf(page_texts: List[str]) -> Dict[str, float]:
    """Computes Inverse Document Frequency (IDF) for terms in a corpus of pages."""
    N = len(page_texts)
    if N == 0:
        return {}
        
    doc_freqs: Dict[str, int] = {}
    for text in page_texts:
        words_set = set(tokenize(text))
        for word in words_set:
            doc_freqs[word] = doc_freqs.get(word, 0) + 1
            
    idf: Dict[str, float] = {}
    for word, df in doc_freqs.items():
        # log(1 + N / (1 + df))
        idf[word] = math.log(1 + N / (1 + df))
    return idf

def compute_tfidf_vector(text: str, idf: Dict[str, float]) -> Dict[str, float]:
    """Computes TF-IDF vector for a single string based on a precalculated IDF vocab."""
    tokens = tokenize(text)
    if not tokens:
        return {}
        
    # Term Frequency (TF)
    tf: Dict[str, float] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0.0) + 1.0
        
    n_tokens = len(tokens)
    tf_idf: Dict[str, float] = {}
    for term, count in tf.items():
        if term in idf:
            # TF = count / n_tokens
            tf_idf[term] = (count / n_tokens) * idf[term]
            
    return tf_idf

# ── Cosine Similarity ──────────────────────────────────────────────────────────
def cosine_similarity(
    vec_a: Union[List[float], Dict[str, float]], 
    vec_b: Union[List[float], Dict[str, float]]
) -> float:
    """Computes cosine similarity for float arrays (HF) or term-weight dicts (TF-IDF)."""
    # Dict case (TF-IDF term-weight dictionaries)
    if isinstance(vec_a, dict) and isinstance(vec_b, dict):
        intersection = set(vec_a.keys()) & set(vec_b.keys())
        numerator = sum(vec_a[t] * vec_b[t] for t in intersection)
        
        sum_a = sum(val ** 2 for val in vec_a.values())
        sum_b = sum(val ** 2 for val in vec_b.values())
        
        if sum_a == 0 or sum_b == 0:
            return 0.0
        return numerator / (math.sqrt(sum_a) * math.sqrt(sum_b))
        
    # List case (dense float vectors from HuggingFace)
    elif isinstance(vec_a, list) and isinstance(vec_b, list):
        if len(vec_a) != len(vec_b) or not vec_a:
            return 0.0
            
        numerator = sum(x * y for x, y in zip(vec_a, vec_b))
        sum_a = sum(x ** 2 for x in vec_a)
        sum_b = sum(y ** 2 for y in vec_b)
        
        if sum_a == 0 or sum_b == 0:
            return 0.0
        return numerator / (math.sqrt(sum_a) * math.sqrt(sum_b))
        
    return 0.0

# ── HuggingFace Inference API Embedding ─────────────────────────────────────────
async def compute_hf_embedding(text: str, token: str) -> Optional[List[float]]:
    """Fetches a dense vector representation using HuggingFace Inference API."""
    url = "https://api-inference.huggingface.co/pipeline/feature-extraction/sentence-transformers/all-MiniLM-L6-v2"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"inputs": text}, headers=headers)
            if response.status_code == 200:
                res = response.json()
                if isinstance(res, list) and len(res) > 0:
                    if isinstance(res[0], list):
                        return res[0]
                    return res
    except Exception as e:
        logger.warning(f"HuggingFace embedding calculation failed: {e}")
    return None

# ── Embedding Processor & Query Interface ───────────────────────────────────────
async def ensure_page_embeddings(doc_id: int, pages: List[Dict[str, Any]]) -> bool:
    """
    Computes and saves embeddings for all pages of a document if not already cached.
    Uses HuggingFace if settings.huggingface_token is set, otherwise falls back to local TF-IDF.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Determine embedding type and model name first
        hf_token = settings.huggingface_token
        use_hf = False
        
        if hf_token:
            logger.info("Attempting HuggingFace embeddings for document...")
            # Test one embedding
            test_vec = await compute_hf_embedding("test text", hf_token)
            if test_vec:
                use_hf = True
                
        model_name = "sentence-transformers/all-MiniLM-L6-v2" if use_hf else "tf-idf"

        cursor.execute("SELECT COUNT(*) FROM page_embeddings WHERE document_id = ? AND model = ?", (doc_id, model_name))
        count = cursor.fetchone()[0]
        if count == len(pages):
            # Already embedded
            return True
            
        # Clear any partial embeddings for this model
        cursor.execute("DELETE FROM page_embeddings WHERE document_id = ? AND model = ?", (doc_id, model_name))
        
        if use_hf:
            for page in pages:
                txt = page.get("text", "").strip() or "Empty page"
                vec = await compute_hf_embedding(txt, hf_token)
                if vec:
                    cursor.execute(
                        "INSERT INTO page_embeddings (document_id, page_number, embedding, model) VALUES (?, ?, ?, ?)",
                        (doc_id, page["page_number"], json.dumps(vec), model_name)
                    )
                else:
                    # Fallback to TF-IDF for this document if any HF calls fail
                    use_hf = False
                    cursor.execute("DELETE FROM page_embeddings WHERE document_id = ? AND model = ?", (doc_id, model_name))
                    model_name = "tf-idf"
                    break
                    
        if not use_hf:
            logger.info("Falling back to local TF-IDF embeddings...")
            # Compute TF-IDF vocab and IDFs across all pages in document
            page_texts = [p.get("text", "") for p in pages]
            idf = compute_tfidf_vocab_and_idf(page_texts)
            
            # Clear any partial TF-IDF embeddings to be safe
            cursor.execute("DELETE FROM page_embeddings WHERE document_id = ? AND model = ?", (doc_id, model_name))
            
            for page in pages:
                txt = page.get("text", "")
                vec = compute_tfidf_vector(txt, idf)
                cursor.execute(
                    "INSERT INTO page_embeddings (document_id, page_number, embedding, model) VALUES (?, ?, ?, ?)",
                    (doc_id, page["page_number"], json.dumps({"vector": vec, "idf": idf}), model_name)
                )
                
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error ensuring page embeddings: {e}")
        return False
    finally:
        conn.close()

async def find_top_k_pages(doc_id: int, query_text: str, k: int = 4) -> List[Dict[str, Any]]:
    """
    Loads cached embeddings for a document, embeds the query,
    calculates cosine similarity, and returns the top-k matches.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        hf_token = settings.huggingface_token
        pref_model = "sentence-transformers/all-MiniLM-L6-v2" if hf_token else "tf-idf"
        
        # Check if we have page embeddings for the preferred model
        cursor.execute("SELECT page_number, embedding FROM page_embeddings WHERE document_id = ? AND model = ?", (doc_id, pref_model))
        rows = cursor.fetchall()
        
        # If not, try the other model
        if not rows:
            other_model = "tf-idf" if pref_model == "sentence-transformers/all-MiniLM-L6-v2" else "sentence-transformers/all-MiniLM-L6-v2"
            cursor.execute("SELECT page_number, embedding FROM page_embeddings WHERE document_id = ? AND model = ?", (doc_id, other_model))
            rows = cursor.fetchall()
            if rows:
                pref_model = other_model
                
        if not rows:
            return []
            
        # Load pages and text
        cursor.execute("SELECT page_number, text FROM pages WHERE document_id = ?", (doc_id,))
        pages_text_map = {p["page_number"]: p["text"] for p in cursor.fetchall()}
        
        # Read the first vector to determine type (list or dict)
        first_vec = json.loads(rows[0]["embedding"])
        
        query_vec = None
        if pref_model == "sentence-transformers/all-MiniLM-L6-v2" and hf_token:
            # Dense floats vector
            query_vec = await compute_hf_embedding(query_text, hf_token)
            
        if query_vec is None:
            # Fallback to TF-IDF (either stored as dict, or dense failed/token missing)
            idf = {}
            for r in rows:
                v = json.loads(r["embedding"])
                if isinstance(v, dict) and "idf" in v:
                    idf = v["idf"]
                    break
            if not idf:
                # Recompute IDF just in case
                page_texts = list(pages_text_map.values())
                idf = compute_tfidf_vocab_and_idf(page_texts)
            query_vec = compute_tfidf_vector(query_text, idf)
            
        scored_pages = []
        for row in rows:
            pnum = row["page_number"]
            vec_json = json.loads(row["embedding"])
            
            # Extract vector content
            if isinstance(vec_json, dict) and "vector" in vec_json:
                p_vec = vec_json["vector"]
            else:
                p_vec = vec_json
                
            sim = cosine_similarity(query_vec, p_vec)
            scored_pages.append({
                "page_number": pnum,
                "text": pages_text_map.get(pnum, ""),
                "similarity": sim
            })
            
        scored_pages.sort(key=lambda x: x["similarity"], reverse=True)
        return scored_pages[:k]
    except Exception as e:
        logger.error(f"Error in find_top_k_pages: {e}")
        return []
    finally:
        conn.close()
