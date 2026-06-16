# Implementation Plan: SyncedNotes AI — Features Roadmap

## Overview

Tasks are ordered by priority: streaming notes first (lowest risk, highest immediate value), then flashcards, highlight & explain, API key manager, chat with RAG, document intelligence, multi-doc synthesis, and finally batch processing. Each task builds on the previous and wires directly into the existing FastAPI + Next.js codebase.

All backend code is Python (FastAPI). All frontend code is TypeScript (React/Next.js). Tests use pytest (backend) and vitest (frontend).

---

## Tasks

- [ ] 1. Extend database schema with all new tables
  - Add `document_intelligence`, `page_embeddings`, `chat_sessions`, `chat_messages`, `flashcards`, `flashcard_collections`, and `batch_jobs` tables to `database.py` inside `init_db()`
  - Add corresponding CRUD helper functions: `save_document_intelligence`, `get_document_intelligence`, `save_page_embeddings`, `get_page_embeddings`, `save_flashcard`, `get_flashcards`, `update_flashcard_sm2`, `save_batch_job`, `get_batch_jobs`, `update_batch_job`
  - _Requirements: 1.7, 2.7, 4.6, 4.7, 6.1, 7.1, 9.1, 17.1_

  - [ ]* 1.1 Write property test for flashcard extraction round-trip
    - **Property 10: Flashcard extraction is a lossless round-trip from practice_questions**
    - Generate random NoteData objects with varying practice_questions arrays; call extraction function; verify front/back/difficulty match 1:1
    - **Validates: Requirements 7.1, 7.2, 7.3**

  - [ ]* 1.2 Write property test for idempotent flashcard generation
    - **Property 11: Flashcard generation is idempotent — no duplicates**
    - Call generate twice on the same document; verify row count is unchanged on second call
    - **Validates: Requirements 7.4**

- [ ] 2. Implement Feature 8: Streaming Notes Generation — backend SSE endpoint
  - Create `backend/streaming.py` with `generate_page_note_stream()` async generator function
  - Implement Groq streaming using `AsyncGroq` with `stream=True` and `response_format={"type": "json_object"}`
  - Implement Gemini streaming using `generate_content_stream` with `response_mime_type="application/json"`
  - Implement Mistral fallback: call existing `call_mistral()` and emit single `{"type": "final", ...}` event
  - Implement field-by-field extraction from accumulating buffer using regex for `title`, `summary`, `tldr`; section detection by matching closing `}` of each sections array element
  - Add `POST /api/generate-page-note/stream` SSE endpoint to `main.py` using `StreamingResponse` with `media_type="text/event-stream"`
  - Handle disconnection via `Request.is_disconnected()`: save partial note if `title` + `summary` are both present in buffer
  - _Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.7, 22.1, 22.2, 22.3, 22.4, 23.1, 23.2, 23.3_

  - [ ]* 2.1 Write property test for streaming event sequence ordering
    - **Property 25: Streaming event sequence is ordered correctly**
    - Mock LLM to return a valid NoteData JSON character-by-character; verify field events precede section events which precede the final event
    - **Validates: Requirements 21.2, 21.3, 21.4, 21.5, 21.6**

  - [ ]* 2.2 Write property test for streaming final event schema completeness
    - **Property 24: Streaming final event produces a complete NoteData object**
    - Generate random valid page texts; stream through mocked Groq/Gemini; verify final event contains all required NoteData fields
    - **Validates: Requirements 21.5, 23.3**

- [ ] 3. Implement Feature 8: Streaming Notes — frontend progressive renderer
  - Add `generateNoteStreaming()` function to `page.tsx` that opens an `EventSource` against `/api/generate-page-note/stream`
  - Handle `field` events: update the relevant field in the page's `NoteState.data.notes` object and trigger re-render
  - Handle `section` events: append to `notes.sections` array
  - Handle `final` event: replace entire note state with validated complete data
  - Handle `error` events: set `NoteState.status = "error"` with the error message
  - Add a `StreamingNoteRenderer` component (or extend existing note renderer) that shows a blinking cursor animation while streaming and a progress indicator based on received fields
  - Wire a `useStreaming` boolean flag to toggle between streaming and non-streaming generation; default to `true` when provider is Groq or Gemini
  - _Requirements: 21.1, 21.2, 21.3, 21.4, 21.5_

- [ ] 4. Checkpoint — Streaming notes working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Implement Feature 3: Flashcard Export & Spaced Repetition — backend
  - Create `backend/flashcards.py`
  - Implement `extract_flashcards_from_notes(doc_id)`: reads all `notes` rows for a document, parses `note_data` JSON, extracts `practice_questions` arrays, inserts into `flashcards` table with duplicate check
  - Implement SM-2 update function `apply_sm2_review(card_id, quality)` following the exact SM-2 algorithm with `easiness` clamped to ≥ 1.3
  - Implement `.apkg` export: create an in-memory SQLite database with Anki's `collection.anki2` schema (cards, notes, decks, col tables), zip with empty `media` file, return as bytes
  - Implement `.csv` export: return UTF-8 CSV with `front,back,difficulty,tags` columns
  - Register endpoints: `POST /api/flashcards/generate/{doc_id}`, `GET /api/flashcards/{doc_id}`, `POST /api/flashcards/review`, `GET /api/flashcards/export/{doc_id}`
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 8.1, 8.2, 8.3, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [ ]* 5.1 Write property test for SM-2 interval monotonicity
    - **Property 12: SM-2 interval is monotonically non-decreasing for repeated correct responses**
    - Generate sequences of N correct reviews (quality ≥ 3) using hypothesis; verify `sm2_interval` is non-decreasing and `sm2_easiness` ≥ 1.3 after each step
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6**

  - [ ]* 5.2 Write property test for due-date filter correctness
    - **Property 13: Due-date filter excludes future cards**
    - Generate flashcards with random `sm2_next_review` dates; call `GET /api/flashcards/{doc_id}?due_only=true`; verify all returned cards have `sm2_next_review ≤ today`
    - **Validates: Requirements 9.7**

  - [ ]* 5.3 Write property test for CSV export row count
    - **Property 14: Flashcard CSV export contains one row per card with correct columns**
    - Generate N random flashcards; export CSV; verify header row + exactly N data rows, and each row matches the source card
    - **Validates: Requirements 8.2**

- [ ] 6. Implement Feature 3: Flashcard — frontend FlashcardPanel
  - Add `FlashcardPanel` component as a modal or drawer accessible from a "Flashcards" button in the toolbar
  - Deck view: fetch cards from `/api/flashcards/{doc_id}`; display front, difficulty badge, due indicator; show "X due today" count
  - Study view: render one card at a time; front is shown; click/tap or spacebar reveals back; three buttons: "Got it" (POST quality=5), "Unsure" (quality=3), "Again" (quality=1); advance to next card after review
  - Export view: two buttons — "Download Anki (.apkg)" and "Download CSV" — triggering file download via `/api/flashcards/export/{doc_id}?format=apkg|csv`
  - _Requirements: 7.1, 8.1, 8.2, 9.1_

- [ ] 7. Checkpoint — Flashcards working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Implement Feature 4: Highlight & Explain — backend endpoint
  - Create `backend/highlight_explain.py`
  - Implement `build_explain_prompt(text, action, context_text)`: selects the correct prompt template per action (`explain`, `define`, `simplify`, `example`); truncates `text` to 500 chars if needed; ensures total prompt ≤ 200 tokens
  - Implement `POST /api/explain` endpoint: validates request, calls `key_manager.get_key("groq")` first, falls back to Gemini then Mistral, returns `{explanation, action, model, provider}`
  - Register endpoint in `main.py`
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7_

  - [ ]* 8.1 Write property test for prompt token bound
    - **Property 15: Explain prompt is token-bounded**
    - Generate random selected texts and action types using hypothesis; call `build_explain_prompt`; verify result length ≤ 800 characters (200 token approximation)
    - **Validates: Requirements 11.5**

  - [ ]* 8.2 Write property test for text truncation at 500 chars
    - **Property 16: Long selected text is truncated to 500 characters**
    - Generate texts of random length > 500 chars; call `build_explain_prompt`; verify the selected_text portion in the output is ≤ 500 chars
    - **Validates: Requirements 11.7**

- [ ] 9. Implement Feature 4: Highlight & Explain — frontend text layer and popover
  - Add `TextLayer` component: a `position: absolute` div overlaid on each page image with `user-select: text; color: transparent; pointer-events: none` (pointer events enabled only for the text itself via a nested span)
  - Populate `TextLayer` with the page text from the `pages` array already in state
  - On `mouseup`, check `window.getSelection().toString().trim()`; if non-empty, show `ExplainMenu` component positioned at `getBoundingClientRect()` of the selection
  - `ExplainMenu`: floating div with four icon-buttons: Explain / Define / Simplify / Example; clicking any button calls `/api/explain` with the action type
  - `ExplainPopover`: card component that appears below the menu, shows spinner then explanation text with model badge; dismisses on outside click or Escape key
  - _Requirements: 10.1, 10.2, 10.3, 11.1, 11.2, 11.3, 11.4_

- [ ] 10. Checkpoint — Highlight & Explain working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. Implement Feature 6: API Key Management — backend
  - Create `backend/key_manager_api.py`
  - Implement `load_keys_json()` and `save_keys_json()` functions for reading/writing `keys.json` in the backend directory
  - Add `reload_from_keys_json()` method to `Settings` class in `config.py` that re-reads `keys.json` and merges with env keys (dedup)
  - Add `rebuild_provider(provider, new_keys)` method to `KeyManager` in `rate_limiter.py`: atomically replaces key list for one provider, creating new `APIKeyInfo` for new keys but preserving `TokenBucket` state for keys that existed before
  - Implement masking function: `mask_key(key)` → first 4 chars + "..." + last 4 chars
  - Implement `probe_key(provider, key)` async function: calls model-list endpoint per provider with 5s timeout
  - Register endpoints: `GET /api/keys/status`, `POST /api/keys`, `DELETE /api/keys/{provider}/{index}`, `POST /api/keys/probe`
  - Add `keys.json` to `.gitignore`
  - _Requirements: 14.1, 14.2, 14.3, 14.4, 15.1, 15.2, 15.3, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6_

  - [ ]* 11.1 Write property test for key masking security
    - **Property 19: API key masking never exposes the full key value**
    - Generate random strings of varying lengths (8–64 chars) as mock keys; call `mask_key`; verify masked string length < original and no contiguous 5+ char substring from the middle appears in output
    - **Validates: Requirements 14.2, 14.3**

  - [ ]* 11.2 Write property test for KeyManager rebuild preserving bucket state
    - **Property 20: KeyManager rebuild preserves unchanged key rate-limit state**
    - Set up a KeyManager with two keys; consume some tokens from key[0]; call `rebuild_provider` adding a third key but keeping key[0]; verify key[0]'s token count is unchanged
    - **Validates: Requirements 15.3**

- [ ] 12. Implement Feature 6: API Key Management — frontend KeyManagerModal
  - Add `KeyManagerModal` component triggered by a gear icon ⚙ in the main toolbar
  - Per-provider sections: Gemini, Groq, Mistral, HuggingFace, Pollinations
  - For each provider: show list of masked keys with delete buttons; show a status badge (🟢 healthy / 🔴 unhealthy / 🟡 untested) and token bucket fill bar from `/api/keys/status`
  - "Add key" form: text input + "Test & Add" button; on click, call `POST /api/keys/probe` first; if `valid: true`, call `POST /api/keys`; show success/error feedback
  - Delete button calls `DELETE /api/keys/{provider}/{index}` and refreshes the status
  - Refresh key status automatically when modal is opened
  - _Requirements: 14.1, 14.2, 14.3, 15.1, 15.2, 16.1, 16.6_

- [ ] 13. Checkpoint — API Key Manager working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 14. Implement Feature 2: Chat with Document — embedding service
  - Create `backend/embeddings.py`
  - Implement `compute_embedding(text, hf_token)`: if HF token available, POST to `https://api-inference.huggingface.co/pipeline/feature-extraction/sentence-transformers/all-MiniLM-L6-v2`; otherwise compute TF-IDF vector using sklearn's `TfidfVectorizer` fit on the page corpus
  - Implement `cosine_similarity(vec_a, vec_b)` using numpy
  - Implement `find_top_k_pages(query_text, page_texts, k=4)`: embed query + all pages (using cache), return top-k page indices by cosine similarity
  - Implement `ensure_page_embeddings(doc_id, pages)`: checks `page_embeddings` table; computes and stores missing embeddings
  - _Requirements: 4.2, 4.3, 6.1, 6.2, 6.3, 6.4_

  - [ ]* 14.1 Write property test for RAG top-k bound
    - **Property 6: Chat RAG retrieval is bounded by top-k**
    - Generate random page corpora (1–100 pages) and random queries; call `find_top_k_pages`; verify `len(result) ≤ top_k` always holds
    - **Validates: Requirements 4.3**

  - [ ]* 14.2 Write property test for embedding caching idempotency
    - **Property 9: Page embedding caching is idempotent**
    - Call `ensure_page_embeddings` twice for the same document; verify the same embedding vectors are returned and only one row per page exists in `page_embeddings`
    - **Validates: Requirements 6.1, 6.2**

- [ ] 15. Implement Feature 2: Chat with Document — backend chat SSE endpoint
  - Create `backend/chat.py`
  - Implement `build_chat_prompt(filename, retrieved_pages, history, message)`: constructs prompt with page context labels, last 4 turns of history, current message; total ≤ 4,000 tokens
  - Implement `POST /api/chat/session` endpoint: creates a new `chat_sessions` row if none exists for the document, returns `session_id`
  - Implement `POST /api/chat/stream` SSE endpoint: runs embedding retrieval, builds prompt, calls LLM with streaming (prefer Groq, fall back to Gemini → Mistral); emits `token`, `citations`, and `done` events; handles rate-limit error with immediate `error` event and stream close; persists messages to `chat_messages` on completion
  - Implement `GET /api/chat/history/{session_id}` endpoint
  - Register all endpoints in `main.py`
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 5.4_

  - [ ]* 15.1 Write property test for chat citations are subset of retrieved pages
    - **Property 7: Chat citations are a subset of retrieved pages**
    - Mock the RAG step to return a known set of pages; call the citation-extraction function; verify every cited page is in the retrieved set
    - **Validates: Requirements 4.5**

  - [ ]* 15.2 Write property test for chat message persistence round-trip
    - **Property 8: Chat message round-trip persistence**
    - Send a message via mock chat stream; call `GET /api/chat/history/{session_id}`; verify both user message and assistant response appear in correct order
    - **Validates: Requirements 4.6, 4.7**

- [ ] 16. Implement Feature 2: Chat with Document — frontend ChatPanel
  - Add `ChatPanel` as a slide-in panel triggered by a "Chat" button in the right-pane toolbar, visible when a document is loaded
  - Message thread: user bubbles right-aligned, assistant bubbles left-aligned; render markdown in assistant messages
  - Streaming: open `EventSource` on send; append tokens to the current assistant message character by character
  - Citation pills below each assistant message: "📄 Pages 3, 7" — clicking a pill scrolls the PDF left pane to that page
  - Session persistence: store `session_id` in component state; on document change, call `POST /api/chat/session` and fetch history from `GET /api/chat/history/{session_id}`
  - Rate-limit error state: show inline banner "Busy — retry in Xs" from the `error` SSE event
  - _Requirements: 4.5, 5.1, 5.2, 5.3_

- [ ] 17. Checkpoint — Chat with Document working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 18. Implement Feature 1: Cross-Page Document Intelligence — backend
  - Create `backend/document_intelligence.py`
  - Implement `extract_page_summary(note_data_json)`: parses NoteData JSON, returns string of `tldr` + `key_concepts[].term` joined, verifying output ≤ 400 characters (100 token proxy)
  - Implement `chunk_pages(page_summaries, chunk_size=20)`: splits list into chunks of at most 20; returns list of chunk strings
  - Implement `generate_document_intelligence(doc_id, model, provider)`:
    - Gate check: raise HTTP 400 if any page lacks a note
    - Build page summaries via `extract_page_summary`
    - If ≤ 60 pages: single aggregation call; if > 60 pages: chunk summarisation then aggregation
    - Prefer Gemini for aggregation call; fall back to Groq (≤ 30 pages) then Mistral
    - Save result to `document_intelligence` table
  - Register `POST /api/document-intelligence` and `GET /api/document-intelligence/{doc_id}` in `main.py`
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1, 3.2, 3.3, 3.4_

  - [ ]* 18.1 Write property test for completeness gate
    - **Property 1: Document intelligence completeness gate**
    - Generate random documents with varying ratios of notes-to-pages; call `generate_document_intelligence`; verify HTTP 400 is returned when any page note is missing
    - **Validates: Requirements 1.1, 1.2**

  - [ ]* 18.2 Write property test for page summary token bound
    - **Property 2: Page summary extraction is token-bounded**
    - Generate random NoteData objects with varying key_concepts arrays; call `extract_page_summary`; verify output length ≤ 400 characters for all inputs
    - **Validates: Requirements 1.3**

  - [ ]* 18.3 Write property test for chunking boundary
    - **Property 3: Chunking respects the 20-page boundary**
    - Generate page counts from 61 to 200; call `chunk_pages`; verify every chunk ≤ 20 entries and the union covers all pages
    - **Validates: Requirements 1.4**

  - [ ]* 18.4 Write property test for DocumentIntelligence output schema
    - **Property 4: DocumentIntelligence output schema completeness**
    - Mock LLM to return valid DocumentIntelligence JSON with all fields; call endpoint; verify response contains all five required fields with correct types
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

  - [ ]* 18.5 Write property test for caching idempotency
    - **Property 5: Document intelligence caching is idempotent**
    - Call generate twice on the same document; verify second call returns `cached: true` with identical field values and `document_intelligence` table has exactly one row
    - **Validates: Requirements 2.6, 2.7**

- [ ] 19. Implement Feature 1: Document Intelligence — frontend panel
  - Add a "Document Summary" button to the toolbar that appears only when all pages have `status === "success"`
  - On click, POST to `/api/document-intelligence` (or GET if already cached) and display a `DocumentIntelligencePanel` on the right side
  - Executive summary: collapsible prose block
  - Concept index: searchable list; clicking a term scrolls the PDF pane to the first referenced page
  - Chapter groups: visual accordion with page range badges
  - Difficulty score: star rating display (1–5) + prerequisite chips
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [ ] 20. Checkpoint — Document Intelligence working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 21. Implement Feature 5: Multi-Document Synthesis — backend
  - Create `backend/synthesis.py`
  - Implement `build_synthesis_prompt(documents, question)`: for each document, extract `tldr` per page and top-10 `key_concepts` terms; label each document section with filename; estimate token count; route to Gemini if > 8,000 tokens, Groq otherwise
  - Implement gate check: return HTTP 400 with unprocessed document list if any `document_id` has no notes
  - Implement `POST /api/synthesise` endpoint: builds prompt, calls routed provider, parses `[Doc N, Page M]` citations from response text, returns `{answer, citations, model, provider}`
  - Register endpoint in `main.py`
  - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 13.1, 13.2, 13.3_

  - [ ]* 21.1 Write property test for synthesis gate
    - **Property 17: Synthesis gate — unprocessed documents cause 400**
    - Generate sets of document IDs where at least one has no notes; call `/api/synthesise`; verify HTTP 400 returned and no LLM call was made
    - **Validates: Requirements 12.6**

  - [ ]* 21.2 Write property test for synthesis prompt size bound
    - **Property 18: Synthesis prompt size is bounded**
    - Generate sets of 2–10 documents each with 1–30 pages; call `build_synthesis_prompt`; verify character count ≤ 128,000 characters for all combinations
    - **Validates: Requirements 12.2, 12.5**

- [ ] 22. Implement Feature 5: Multi-Document Synthesis — frontend SynthesisPanel
  - Add `SynthesisPanel` accessible via a "Synthesise" button in the document library toolbar (only visible when ≥ 2 documents are loaded)
  - Step 1: multi-select checkboxes over the previous books list (2–10 documents selectable)
  - Step 2: question text area with placeholder suggestions
  - Step 3: rendered answer with citation chips (e.g., "📄 Doc 1 — Intro to ML, Pages 3, 7")
  - Clicking a citation chip opens that document and scrolls to the cited page
  - _Requirements: 12.1, 12.4, 13.1, 13.2_

- [ ] 23. Checkpoint — Multi-Document Synthesis working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 24. Implement Feature 7: Background Batch Processing — BatchWorker and inbox watcher
  - Create `backend/batch_queue.py`
  - Implement `BatchWorker` class as an `asyncio.Task`: on startup, query `batch_jobs` for `status IN ('queued', 'processing')` and resume; main loop processes one page at a time, calls `key_manager.get_key()`, sleeps `seconds_until_available()` when all keys exhausted, waits ≥ 2s between pages
  - Implement `InboxWatcher`: try `watchdog` import first, fall back to 30s polling; on new `.pdf` file, call `enqueue_pdf(path, model, provider)`
  - Implement `enqueue_pdf(file_path, model, provider)`: inserts a `batch_jobs` row with `status='queued'`
  - Implement WebSocket `/ws/batch-progress`: on connect, send `{"type": "snapshot", "jobs": [...]}` then push progress events as batch worker broadcasts them via an `asyncio.Queue`
  - Register `GET /api/batch/jobs`, `POST /api/batch/add`, `DELETE /api/batch/jobs/{job_id}` endpoints in `main.py`
  - Start `BatchWorker` and `InboxWatcher` in the FastAPI `startup_event`
  - _Requirements: 17.1, 17.2, 17.3, 18.1, 18.2, 18.3, 18.4, 19.1, 19.2, 19.3, 19.4, 20.1, 20.2, 20.3, 20.4_

  - [ ]* 24.1 Write property test for batch job completion totals
    - **Property 21: Batch job completion totals are exact**
    - Simulate a batch job to completion with mocked note generation; verify `completed_pages === total_pages` and `notes` table has `total_pages` rows for the document
    - **Validates: Requirements 17.1, 18.4**

  - [ ]* 24.2 Write property test for inter-page gap enforcement
    - **Property 22: Batch worker inter-page gap is at least 2 seconds**
    - Mock `asyncio.sleep` and timestamp calls; run batch worker over a 3-page document; verify the sleep between page 1→2 and 2→3 is ≥ 2 seconds each
    - **Validates: Requirements 18.3**

  - [ ]* 24.3 Write property test for WebSocket broadcast completeness
    - **Property 23: Batch WebSocket broadcasts page completion for every processed page**
    - Run a batch job with N pages; collect all WebSocket messages; verify exactly N `page_done` messages were emitted, one per page
    - **Validates: Requirements 20.1, 20.4**

- [ ] 25. Implement Feature 7: Background Batch Processing — frontend BatchQueuePanel
  - Add `BatchQueuePanel` as a drawer triggered by a "Batch" button in the toolbar
  - Job list: show filename, status badge, progress bar (completed_pages / total_pages), timestamps
  - "Add PDF" form: file picker + model/provider selectors; POST to `/api/batch/add`
  - Connect to `/ws/batch-progress` WebSocket on panel open; update job list in real time from `page_done` and `job_done` events
  - Show rate-limit indicator: "~3s/page with current keys" computed from `seconds_until_available`
  - Cancel/remove button per job: calls `DELETE /api/batch/jobs/{job_id}`
  - _Requirements: 19.4, 20.1, 20.2, 20.4_

- [ ] 26. Final checkpoint — All features working end-to-end
  - Ensure all tests pass, ask the user if questions arise.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1"]
    },
    {
      "wave": 2,
      "tasks": ["2", "5", "8", "11", "14"]
    },
    {
      "wave": 3,
      "tasks": ["3", "6", "9", "12", "15"]
    },
    {
      "wave": 4,
      "tasks": ["4", "7", "10", "13", "16", "18", "21"]
    },
    {
      "wave": 5,
      "tasks": ["17", "19", "22", "24"]
    },
    {
      "wave": 6,
      "tasks": ["20", "23", "25"]
    },
    {
      "wave": 7,
      "tasks": ["26"]
    }
  ]
}
```

All feature tasks depend on Task 1 (schema). Within each feature group, the backend task must complete before the frontend task. Feature groups are otherwise independent and can be built in parallel by different developers.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP iteration
- Build order is by priority: Feature 8 (streaming) → Feature 3 (flashcards) → Feature 4 (explain) → Feature 6 (keys) → Feature 2 (chat) → Feature 1 (doc intelligence) → Feature 5 (synthesis) → Feature 7 (batch)
- Each feature introduces a new backend file; `main.py` only imports and registers the new router/endpoint
- All new backend modules should be importable with the same try/except `backend.module` / `module` pattern as `main.py`
- The `keys.json` file must be added to `.gitignore` before merging any branch that includes Feature 6
- Property tests use `hypothesis` (Python) and `fast-check` (TypeScript/frontend)
