import json
import logging
import sqlite3
import zipfile
import io
import csv
import zlib
import time
import random
import tempfile
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse

try:
    from backend.database import (
        get_db_connection, save_flashcard, get_flashcards, update_flashcard_sm2,
        get_flashcard_collections, save_flashcard_collection
    )
except ModuleNotFoundError:
    from database import (
        get_db_connection, save_flashcard, get_flashcards, update_flashcard_sm2,
        get_flashcard_collections, save_flashcard_collection
    )

logger = logging.getLogger("SyncedNotesAI.Flashcards")
router = APIRouter(prefix="/api/flashcards")

def generate_anki_guid() -> str:
    """Helper to generate a unique 10-character guid for Anki."""
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(chars) for _ in range(10))

def build_apkg_bytes(cards_list: List[Dict[str, Any]], deck_name: str) -> bytes:
    """
    Creates a temporary SQLite database populated with Anki's schemas,
    converts it to Anki .apkg format (zipped with empty media file), and returns the bytes.
    """
    # Create a temporary file path
    temp_dir = tempfile.gettempdir()
    temp_db_path = os.path.join(temp_dir, f"anki_{random.randint(0, 1000000)}.db")
    
    conn = sqlite3.connect(temp_db_path)
    cursor = conn.cursor()
    
    # Create Anki schemas
    cursor.execute("""
        CREATE TABLE col (
            id              integer primary key,
            crt             integer not null,
            mod             integer not null,
            scm             integer not null,
            ver             integer not null,
            dty             integer not null,
            usn             integer not null,
            ls              integer not null,
            conf            text not null,
            models          text not null,
            decks           text not null,
            dconf           text not null,
            tags            text not null
        )
    """)
    cursor.execute("""
        CREATE TABLE notes (
            id              integer primary key,
            guid            text not null,
            mid             integer not null,
            mod             integer not null,
            usn             integer not null,
            tags            text not null,
            flds            text not null,
            sfld            text not null,
            csum            integer not null,
            flags           integer not null,
            data            text not null
        )
    """)
    cursor.execute("""
        CREATE TABLE cards (
            id              integer primary key,
            nid             integer not null,
            did             integer not null,
            ord             integer not null,
            mod             integer not null,
            usn             integer not null,
            type            integer not null,
            queue           integer not null,
            due             integer not null,
            ivl             integer not null,
            factor          integer not null,
            reps            integer not null,
            lapses          integer not null,
            left            integer not null,
            odue            integer not null,
            odid            integer not null,
            flags           integer not null,
            data            text not null
        )
    """)
    cursor.execute("""
        CREATE TABLE revlog (
            id              integer primary key,
            cid             integer not null,
            usn             integer not null,
            ease            integer not null,
            ivl             integer not null,
            lastIvl         integer not null,
            factor          integer not null,
            time            integer not null,
            type            integer not null
        )
    """)

    # Populate Metadata / configs
    now = int(time.time())
    deck_id = 1404106540307
    model_id = 1404106540306
    
    conf = {
        "nextPos": 1, "estTimes": True, "activeDecks": [1, deck_id],
        "addToCur": True, "curDeck": deck_id, "newBrd": True,
        "sortType": "noteFld", "sortBackwards": False, "collapseTime": 1200,
        "timeLim": 0, "curModel": model_id, "repDays": 7
    }
    
    decks = {
        "1": {"id": 1, "mod": 0, "name": "Default", "desc": "", "collapsed": False, "browserCollapsed": False, "newToday": [0, 0], "revToday": [0, 0], "lrnToday": [0, 0], "timeToday": [0, 0], "conf": 1, "usn": 0, "dyn": 0},
        str(deck_id): {"id": deck_id, "mod": now, "name": deck_name, "desc": "Generated from SyncedNotes AI", "collapsed": False, "browserCollapsed": False, "newToday": [0, 0], "revToday": [0, 0], "lrnToday": [0, 0], "timeToday": [0, 0], "conf": 1, "usn": 0, "dyn": 0}
    }
    
    dconf = {
        "1": {"id": 1, "mod": 0, "name": "Default", "maxTaken": 60, "autoplay": True, "replayq": True, "new": {"delays": [1.0, 10.0], "ints": [1, 4, 0], "initialFactor": 2500, "order": 1, "perDay": 20, "bury": False}, "rev": {"perDay": 200, "ease4": 1.3, "fuzz": 0.05, "minSpace": 1, "ivlFct": 1.0, "maxIvl": 36500, "bury": False}, "lapse": {"delays": [10.0], "mult": 0.0, "minInt": 1, "leechFails": 8, "leechAction": 0}, "usn": 0}
    }
    
    models = {
        str(model_id): {
            "id": model_id, "name": "Basic (SyncedNotes)",
            "flds": [
                {"name": "Front", "ord": 0, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []},
                {"name": "Back", "ord": 1, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []}
            ],
            "tmpls": [
                {"name": "Card 1", "ord": 0, "qfmt": "{{Front}}", "afmt": "{{Front}}\n\n<hr id=answer>\n\n{{Back}}", "did": None, "bafmt": "", "bqfmt": ""}
            ],
            "css": ".card {\n font-family: arial;\n font-size: 20px;\n text-align: center;\n color: black;\n background-color: white;\n}\n",
            "mod": now, "usn": 0, "type": 0, "vers": []
        }
    }

    cursor.execute("""
        INSERT INTO col (id, crt, mod, scm, ver, dty, usn, ls, conf, models, decks, dconf, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (1, now, now, now, 11, 0, 0, now, json.dumps(conf), json.dumps(models), json.dumps(decks), json.dumps(dconf), "{}"))

    # Insert cards
    for idx, card in enumerate(cards_list):
        card_id = 1600000000000 + (idx * 1000) + random.randint(0, 999)
        note_id = 1700000000000 + (idx * 1000) + random.randint(0, 999)
        guid = generate_anki_guid()
        
        front = card["front"]
        back = card["back"]
        
        flds = f"{front}\x1f{back}"
        sfld = front[:100]
        csum = zlib.adler32(front.encode("utf-8")) & 0xffffffff

        # Save note
        cursor.execute("""
            INSERT INTO notes (id, guid, mid, mod, usn, tags, flds, sfld, csum, flags, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (note_id, guid, model_id, now, 0, "", flds, sfld, csum, 0, ""))

        # Save card
        cursor.execute("""
            INSERT INTO cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, factor, reps, lapses, left, odue, odid, flags, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (card_id, note_id, deck_id, 0, now, 0, 0, 0, idx, 0, 2500, 0, 0, 0, 0, 0, 0, ""))

    conn.commit()
    conn.close()
    
    # Read the temporary database file contents
    with open(temp_db_path, "rb") as f:
        db_data = f.read()
        
    # Remove the temporary database file
    try:
        os.remove(temp_db_path)
    except Exception as e:
        logger.warning(f"Failed to delete temp db file {temp_db_path}: {e}")
    
    # Zip package
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("collection.anki2", db_data)
        zip_file.writestr("media", "{}")
        
    return zip_buffer.getvalue()


@router.post("/generate/{doc_id}")
async def generate_flashcards(doc_id: int):
    """
    Extracts practice questions from notes table for a document
    and populates the flashcards table, filtering out duplicates.
    """
    conn = get_db_connection()
    generated = 0
    skipped = 0
    try:
        cursor = conn.cursor()
        # Fetch all notes
        cursor.execute("SELECT note_data, page_number FROM notes WHERE document_id = ?", (doc_id,))
        rows = cursor.fetchall()
        if not rows:
            return {"generated": 0, "skipped": 0, "total": 0}
            
        # Get existing flashcard fronts to avoid duplicates
        cursor.execute("SELECT front FROM flashcards WHERE document_id = ?", (doc_id,))
        existing_fronts = {r["front"] for r in cursor.fetchall()}
        
        for r in rows:
            page_num = r["page_number"]
            try:
                note_data = json.loads(r["note_data"])
                questions = note_data.get("practice_questions", [])
                for q in questions:
                    front = q.get("question", "").strip()
                    back = q.get("answer", "").strip()
                    difficulty = q.get("difficulty", "basic").strip()
                    
                    if not front or not back:
                        continue
                        
                    if front in existing_fronts:
                        skipped += 1
                        continue
                        
                    save_flashcard(doc_id, page_num, front, back, difficulty)
                    existing_fronts.add(front)
                    generated += 1
            except Exception as e:
                logger.error(f"Error parsing note practice questions: {e}")
                
        total = len(existing_fronts)
        return {"generated": generated, "skipped": skipped, "total": total}
    except Exception as e:
        logger.error(f"Failed to generate flashcards: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/{doc_id}")
async def fetch_flashcards(doc_id: int, due_only: bool = False):
    """Retrieves flashcards for a document, optionally filtered by review due date."""
    try:
        cards = get_flashcards(doc_id, due_only=due_only)
        return {"cards": cards}
    except Exception as e:
        logger.error(f"Error fetching flashcards: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ReviewRequest(Dict[str, Any]):
    # Subclassing dict to handle standard JSON parsing safely
    pass

@router.post("/review")
async def review_flashcard(req: Dict[str, Any]):
    """
    Submits a review quality score (0-5) for a card, recalculating SM-2 scheduler values.
    """
    card_id = req.get("card_id")
    quality = req.get("quality")
    if card_id is None or quality is None:
        raise HTTPException(status_code=400, detail="Missing card_id or quality")
        
    if not (0 <= quality <= 5):
        raise HTTPException(status_code=400, detail="Quality must be between 0 and 5")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM flashcards WHERE id = ?", (card_id,))
        card = cursor.fetchone()
        if not card:
            raise HTTPException(status_code=404, detail="Flashcard not found")

        n = card["sm2_n"]
        easiness = card["sm2_easiness"]
        interval = card["sm2_interval"]

        # SM-2 Spaced Repetition Logic
        if quality >= 3:
            if n == 0:
                interval = 1
            elif n == 1:
                interval = 6
            else:
                interval = int(round(interval * easiness))
            
            easiness = easiness + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
            n += 1
        else:
            n = 0
            interval = 1
            # easiness unchanged but n reset
            
        easiness = max(1.3, easiness)
        
        # Calculate next review date
        next_review_date = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
        
        update_flashcard_sm2(card_id, n, easiness, interval, next_review_date)
        
        return {
            "card_id": card_id,
            "sm2_n": n,
            "sm2_easiness": easiness,
            "sm2_interval": interval,
            "sm2_next_review": next_review_date
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reviewing flashcard: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/export/{doc_id}")
async def export_flashcards(doc_id: int, format: str = "apkg"):
    """
    Exports flashcards for a document as either Anki Package (.apkg) or CSV.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM documents WHERE id = ?", (doc_id,))
        doc = cursor.fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
            
        filename = doc["filename"]
        cards = get_flashcards(doc_id, due_only=False)
        
        if not cards:
            raise HTTPException(status_code=400, detail="No flashcards generated for this document")
            
        if format == "apkg":
            deck_name = filename.rsplit(".", 1)[0]
            apkg_data = build_apkg_bytes(cards, deck_name)
            
            headers = {
                "Content-Disposition": f'attachment; filename="flashcards-{doc_id}.apkg"'
            }
            return Response(content=apkg_data, media_type="application/octet-stream", headers=headers)
            
        elif format == "csv":
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            # Write Header
            writer.writerow(["front", "back", "difficulty", "tags"])
            
            # Clean tag
            tag = filename.replace(" ", "_").replace(",", "")
            
            for c in cards:
                writer.writerow([c["front"], c["back"], c["difficulty"], tag])
                
            headers = {
                "Content-Disposition": f'attachment; filename="flashcards-{doc_id}.csv"'
            }
            return Response(content=output.getvalue(), media_type="text/csv", headers=headers)
            
        else:
            raise HTTPException(status_code=400, detail="Invalid export format. Supported formats: apkg, csv")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export flashcards: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
