import sqlite3
import os
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

# Log setup
logger = logging.getLogger("SyncedNotesAI.Database")

# DB path in the root workspace directory
DB_PATH = Path(__file__).resolve().parent.parent / "syncednotes.db"

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """
    Initializes the SQLite tables in syncednotes.db inside the root directory.
    """
    logger.info(f"Initializing local SQLite database at: {DB_PATH}")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Documents Table (keeps SHA-256 hash of PDF content)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hash TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Pages Table (stores parsed text page-by-page)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        
        # Notes Table (stores generated note JSON structure, mind map, and SVG infographic)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                note_data TEXT NOT NULL,      -- JSON string of notes
                mind_map TEXT,               -- Mermaid syntax
                infographic TEXT,            -- SVG XML string
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, page_number)
            )
        """)
        
        # Document Intelligence Table (Feature 1)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS document_intelligence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL UNIQUE,
                executive_summary TEXT NOT NULL,
                concept_index TEXT NOT NULL,        -- JSON array
                chapter_groups TEXT NOT NULL,       -- JSON array
                difficulty_score INTEGER NOT NULL,
                prerequisite_knowledge TEXT NOT NULL, -- JSON array
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)

        # Page Embeddings Table (Feature 2)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS page_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                embedding TEXT NOT NULL,   -- JSON float array
                model TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, page_number, model)
            )
        """)

        # Chat Sessions (Feature 2)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)

        # Chat Messages (Feature 2)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                cited_pages TEXT,          -- JSON array of ints
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            )
        """)

        # Flashcards Table (Feature 3)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS flashcards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                front TEXT NOT NULL,
                back TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                sm2_n INTEGER DEFAULT 0,
                sm2_easiness REAL DEFAULT 2.5,
                sm2_interval INTEGER DEFAULT 1,
                sm2_next_review TEXT DEFAULT (date('now')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)

        # Flashcard Collections (Feature 3)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS flashcard_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                document_ids TEXT NOT NULL,    -- JSON array of document IDs
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Batch Jobs Table (Feature 7)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                total_pages INTEGER DEFAULT 0,
                completed_pages INTEGER DEFAULT 0,
                error_message TEXT,
                model TEXT,
                provider TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        
        conn.commit()
        logger.info("Database schemas verified/created successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        raise e
    finally:
        conn.close()


def get_document_by_hash(doc_hash: str) -> Optional[Dict[str, Any]]:
    """
    Looks up a document and its pages by SHA-256 hash.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, filename FROM documents WHERE hash = ?", (doc_hash,))
        doc_row = cursor.fetchone()
        if not doc_row:
            return None
        
        doc_id = doc_row["id"]
        filename = doc_row["filename"]
        
        cursor.execute("SELECT page_number, text FROM pages WHERE document_id = ? ORDER BY page_number ASC", (doc_id,))
        page_rows = cursor.fetchall()
        
        pages = [{"page_number": r["page_number"], "text": r["text"]} for r in page_rows]
        return {
            "id": doc_id,
            "filename": filename,
            "pages": pages
        }
    except Exception as e:
        logger.error(f"Error fetching document by hash: {str(e)}")
        return None
    finally:
        conn.close()


def save_document(doc_hash: str, filename: str, pages: List[Dict[str, Any]]) -> int:
    """
    Saves a new document and its parsed pages, returning the new document ID.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO documents (hash, filename) VALUES (?, ?)",
            (doc_hash, filename)
        )
        doc_id = cursor.lastrowid
        
        # Insert pages
        page_data = [(doc_id, p["page_number"], p["text"]) for p in pages]
        cursor.executemany(
            "INSERT INTO pages (document_id, page_number, text) VALUES (?, ?, ?)",
            page_data
        )
        
        conn.commit()
        logger.info(f"Saved new document to DB: {filename} (ID: {doc_id})")
        return doc_id
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save document to database: {str(e)}")
        raise e
    finally:
        conn.close()


def get_note(doc_id: int, page_number: int) -> Optional[Dict[str, Any]]:
    """
    Fetches cached notes, mind map, and infographic for a specific document page.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT model, provider, note_data, mind_map, infographic FROM notes WHERE document_id = ? AND page_number = ?",
            (doc_id, page_number)
        )
        row = cursor.fetchone()
        if row:
            return {
                "model": row["model"],
                "provider": row["provider"],
                "note_data": row["note_data"],
                "mind_map": row["mind_map"],
                "infographic": row["infographic"]
            }
        return None
    except Exception as e:
        logger.error(f"Failed to fetch note from database: {str(e)}")
        return None
    finally:
        conn.close()


def save_note(doc_id: int, page_number: int, model: str, provider: str, note_data: str, mind_map: str, infographic: str):
    """
    Saves or updates a page note cache.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO notes (document_id, page_number, model, provider, note_data, mind_map, infographic)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id, page_number) DO UPDATE SET
                model = excluded.model,
                provider = excluded.provider,
                note_data = excluded.note_data,
                mind_map = excluded.mind_map,
                infographic = excluded.infographic
        """, (doc_id, page_number, model, provider, note_data, mind_map, infographic))
        conn.commit()
        logger.info(f"Saved/Updated note cache for Document {doc_id}, Page {page_number}")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to cache note: {str(e)}")
        raise e
    finally:
        conn.close()


def delete_note(doc_id: int, page_number: int) -> bool:
    """
    Deletes the cached note for a specific document page so it can be regenerated.
    Returns True if a row was deleted, False if nothing was found.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM notes WHERE document_id = ? AND page_number = ?",
            (doc_id, page_number)
        )
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"Deleted cached note for Document {doc_id}, Page {page_number}")
        return deleted
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to delete note: {str(e)}")
        return False
    finally:
        conn.close()


def get_document_hash(doc_id: int) -> Optional[str]:
    """
    Retrieves the unique SHA-256 hash for a document by its ID.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT hash FROM documents WHERE id = ?", (doc_id,))
        row = cursor.fetchone()
        if row:
            return row["hash"]
        return None
    except Exception as e:
        logger.error(f"Failed to fetch document hash: {str(e)}")
        return None
    finally:
        conn.close()


def list_documents() -> List[Dict[str, Any]]:
    """
    Retrieves all previously parsed documents sorted by creation date.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, filename, hash, created_at FROM documents ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to list documents: {str(e)}")
        return []
    finally:
        conn.close()


def get_document_by_id(doc_id: int) -> Optional[Dict[str, Any]]:
    """
    Retrieves a document's metadata along with its pages and cached notes/diagrams.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Get document metadata
        cursor.execute("SELECT id, filename, hash, created_at FROM documents WHERE id = ?", (doc_id,))
        doc_row = cursor.fetchone()
        if not doc_row:
            return None
        
        document = dict(doc_row)
        
        # Get related pages
        cursor.execute(
            "SELECT page_number, text FROM pages WHERE document_id = ? ORDER BY page_number ASC",
            (doc_id,)
        )
        pages_rows = cursor.fetchall()
        
        # Format pages
        pages = []
        for r in pages_rows:
            p_num = r["page_number"]
            # Fetch cached note if exists
            cursor.execute(
                "SELECT model, provider, note_data, mind_map, infographic FROM notes WHERE document_id = ? AND page_number = ?",
                (doc_id, p_num)
            )
            note_row = cursor.fetchone()
            cached_note = None
            if note_row:
                cached_note = {
                    "model": note_row["model"],
                    "provider": note_row["provider"],
                    "note_data": note_row["note_data"],
                    "mind_map": note_row["mind_map"],
                    "infographic": note_row["infographic"]
                }
            pages.append({
                "page_number": p_num,
                "text": r["text"],
                "cached_note": cached_note
            })
        
        document["pages"] = pages
        return document
    except Exception as e:
        logger.error(f"Error fetching document by ID: {str(e)}")
        return None
    finally:
        conn.close()


def save_document_intelligence(doc_id: int, summary: str, concepts: str, chapters: str, diff_score: int, prerequisites: str, model: str, provider: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO document_intelligence (document_id, executive_summary, concept_index, chapter_groups, difficulty_score, prerequisite_knowledge, model, provider)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                executive_summary = excluded.executive_summary,
                concept_index = excluded.concept_index,
                chapter_groups = excluded.chapter_groups,
                difficulty_score = excluded.difficulty_score,
                prerequisite_knowledge = excluded.prerequisite_knowledge,
                model = excluded.model,
                provider = excluded.provider
        """, (doc_id, summary, concepts, chapters, diff_score, prerequisites, model, provider))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save document intelligence: {str(e)}")
        raise e
    finally:
        conn.close()


def get_document_intelligence(doc_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM document_intelligence WHERE document_id = ?", (doc_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Failed to get document intelligence: {str(e)}")
        return None
    finally:
        conn.close()


def save_page_embedding(doc_id: int, page_num: int, embedding: str, model: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO page_embeddings (document_id, page_number, embedding, model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(document_id, page_number, model) DO UPDATE SET
                embedding = excluded.embedding
        """, (doc_id, page_num, embedding, model))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save page embedding: {str(e)}")
        raise e
    finally:
        conn.close()


def get_page_embeddings(doc_id: int, model: str) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT page_number, embedding FROM page_embeddings WHERE document_id = ? AND model = ?", (doc_id, model))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to get page embeddings: {str(e)}")
        return []
    finally:
        conn.close()


def create_chat_session(doc_id: int) -> int:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO chat_sessions (document_id) VALUES (?)", (doc_id,))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to create chat session: {str(e)}")
        raise e
    finally:
        conn.close()


def get_chat_session_by_doc(doc_id: int) -> Optional[int]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM chat_sessions WHERE document_id = ? ORDER BY created_at DESC LIMIT 1", (doc_id,))
        row = cursor.fetchone()
        return row["id"] if row else None
    except Exception as e:
        logger.error(f"Failed to get chat session by doc: {str(e)}")
        return None
    finally:
        conn.close()


def save_chat_message(session_id: int, role: str, content: str, cited_pages: Optional[str] = None):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chat_messages (session_id, role, content, cited_pages)
            VALUES (?, ?, ?, ?)
        """, (session_id, role, content, cited_pages))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save chat message: {str(e)}")
        raise e
    finally:
        conn.close()


def get_chat_history(session_id: int) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, role, content, cited_pages, created_at FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC", (session_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to get chat history: {str(e)}")
        return []
    finally:
        conn.close()


def save_flashcard(doc_id: int, page_num: int, front: str, back: str, difficulty: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO flashcards (document_id, page_number, front, back, difficulty)
            VALUES (?, ?, ?, ?, ?)
        """, (doc_id, page_num, front, back, difficulty))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save flashcard: {str(e)}")
        raise e
    finally:
        conn.close()


def get_flashcards(doc_id: int, due_only: bool = False) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if due_only:
            cursor.execute("""
                SELECT * FROM flashcards 
                WHERE document_id = ? AND date(sm2_next_review) <= date('now')
                ORDER BY page_number ASC
            """, (doc_id,))
        else:
            cursor.execute("SELECT * FROM flashcards WHERE document_id = ? ORDER BY page_number ASC", (doc_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to get flashcards: {str(e)}")
        return []
    finally:
        conn.close()


def update_flashcard_sm2(card_id: int, n: int, easiness: float, interval: int, next_review: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE flashcards 
            SET sm2_n = ?, sm2_easiness = ?, sm2_interval = ?, sm2_next_review = ?
            WHERE id = ?
        """, (n, easiness, interval, next_review, card_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update flashcard SM-2: {str(e)}")
        raise e
    finally:
        conn.close()


def get_flashcard_collections() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM flashcard_collections ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to get flashcard collections: {str(e)}")
        return []
    finally:
        conn.close()


def save_flashcard_collection(name: str, doc_ids: str) -> int:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO flashcard_collections (name, document_ids) VALUES (?, ?)", (name, doc_ids))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save flashcard collection: {str(e)}")
        raise e
    finally:
        conn.close()


def save_batch_job(filename: str, file_path: str, model: str, provider: str) -> int:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO batch_jobs (filename, file_path, model, provider, status)
            VALUES (?, ?, ?, ?, 'queued')
        """, (filename, file_path, model, provider))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save batch job: {str(e)}")
        raise e
    finally:
        conn.close()


def get_batch_jobs() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM batch_jobs ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to get batch jobs: {str(e)}")
        return []
    finally:
        conn.close()


def get_batch_job(job_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM batch_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Failed to get batch job: {str(e)}")
        return None
    finally:
        conn.close()


def update_batch_job(job_id: int, status: str, document_id: Optional[int] = None, total_pages: Optional[int] = None, completed_pages: Optional[int] = None, error_message: Optional[str] = None, started_at: Optional[str] = None, completed_at: Optional[str] = None):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        updates = []
        params = []
        
        updates.append("status = ?")
        params.append(status)
        
        if document_id is not None:
            updates.append("document_id = ?")
            params.append(document_id)
            
        if total_pages is not None:
            updates.append("total_pages = ?")
            params.append(total_pages)
            
        if completed_pages is not None:
            updates.append("completed_pages = ?")
            params.append(completed_pages)
            
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
            
        if started_at is not None:
            updates.append("started_at = ?")
            params.append(started_at)
            
        if completed_at is not None:
            updates.append("completed_at = ?")
            params.append(completed_at)
            
        params.append(job_id)
        cursor.execute(f"UPDATE batch_jobs SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update batch job: {str(e)}")
        raise e
    finally:
        conn.close()

