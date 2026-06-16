# Requirements Document

## Introduction

This document specifies the functional requirements for eight feature groups that extend SyncedNotes AI from a per-page study assistant into a full document intelligence platform. All features are additive — they do not break the existing upload, note generation, mind map, or infographic flows. Requirements are derived directly from the technical design and are expressed using EARS patterns.

---

## Glossary

- **Document_Intelligence_Service**: The backend subsystem that generates executive summaries, concept indices, chapter groups, and difficulty scores for a complete document.
- **Chat_Service**: The backend subsystem that handles RAG-based chat sessions with streaming SSE responses.
- **Flashcard_Service**: The backend subsystem responsible for extracting flashcards from cached notes and managing SM-2 spaced repetition state.
- **Explain_Service**: The backend subsystem that handles highlight-and-explain requests for selected text.
- **Synthesis_Service**: The backend subsystem that aggregates summaries from multiple documents to answer cross-document questions.
- **Key_Manager**: The backend subsystem responsible for runtime management of API keys stored in `keys.json`.
- **Batch_Worker**: The background asyncio task that processes queued PDF jobs respecting rate limits.
- **Streaming_Service**: The backend subsystem that delivers partial note JSON to the frontend via SSE as the LLM generates tokens.
- **KeyManager**: The existing `rate_limiter.py` `KeyManager` class and singleton `key_manager`.
- **Embedding_Service**: The local embedding component in `embeddings.py` using sentence-transformers or TF-IDF fallback.
- **System**: The combined FastAPI backend and Next.js frontend of SyncedNotes AI.
- **NoteData**: The JSON object stored per page in the `notes` table, containing `title`, `summary`, `tldr`, `key_concepts`, `sections`, `formulas_and_rules`, `practice_questions`, `common_mistakes`, and `brainstorming_ideas`.
- **SM-2**: The SuperMemo-2 spaced repetition algorithm used for flashcard scheduling.
- **SSE**: Server-Sent Events — a unidirectional HTTP streaming protocol used for token-by-token delivery.
- **RAG**: Retrieval-Augmented Generation — the technique of injecting retrieved context into an LLM prompt to ground its responses.
- **top_k**: The maximum number of pages retrieved by the embedding similarity search for RAG context (default 4).
- **inbox**: The local filesystem directory monitored by the Batch_Worker for new PDF files.
- **keys.json**: The local JSON file in the backend directory where API keys added via the UI are persisted.

---

## Requirements

### Requirement 1: Cross-Page Document Intelligence — Completeness Gate

**User Story:** As a student, I want the system to generate a document-level executive summary only after all pages are processed, so that the summary is complete and accurate.

#### Acceptance Criteria

1. WHEN a request is made to `POST /api/document-intelligence`, IF any page in the document does not have a cached note in the `notes` table, THEN THE Document_Intelligence_Service SHALL return an HTTP 400 response with a message indicating how many pages remain unprocessed.
2. WHEN all pages of a document have cached notes, THE Document_Intelligence_Service SHALL accept a `POST /api/document-intelligence` request and begin processing.
3. THE Document_Intelligence_Service SHALL extract only the `tldr` and `key_concepts[].term` fields from each page's `NoteData` to build the aggregation input, producing at most 100 tokens per page.
4. WHEN a document has more than 60 pages, THE Document_Intelligence_Service SHALL divide pages into chunks of 20, summarise each chunk independently, then aggregate chunk summaries in a final call.
5. WHEN a document has 60 or fewer pages, THE Document_Intelligence_Service SHALL aggregate all page summaries in a single LLM call.

### Requirement 2: Cross-Page Document Intelligence — Output

**User Story:** As a student, I want the document intelligence results to include an executive summary, concept index, chapter groupings, and a difficulty rating, so that I can understand the document's structure and scope at a glance.

#### Acceptance Criteria

1. THE Document_Intelligence_Service SHALL produce an `executive_summary` field containing 3 to 5 prose paragraphs summarising the entire document.
2. THE Document_Intelligence_Service SHALL produce a `concept_index` field as a JSON array where each entry contains a `term`, a `definition`, and a `pages` array listing the page numbers where that term appears.
3. THE Document_Intelligence_Service SHALL produce a `chapter_groups` field as a JSON array where each entry contains a `title`, `page_start`, `page_end`, and `summary` representing a detected topic cluster.
4. THE Document_Intelligence_Service SHALL produce a `difficulty_score` integer between 1 and 5, where 1 represents introductory content and 5 represents advanced content.
5. THE Document_Intelligence_Service SHALL produce a `prerequisite_knowledge` array of strings listing inferred prior knowledge requirements.
6. WHEN a cached result exists in the `document_intelligence` table, THE Document_Intelligence_Service SHALL return it immediately with `cached: true` without making any LLM calls.
7. THE Document_Intelligence_Service SHALL store the result in the `document_intelligence` table with `ON CONFLICT DO UPDATE` semantics.

### Requirement 3: Cross-Page Document Intelligence — Provider Routing

**User Story:** As a user, I want the system to use the most capable available model for document-level analysis, so that the quality is as high as possible.

#### Acceptance Criteria

1. WHEN Gemini keys are available and healthy, THE Document_Intelligence_Service SHALL prefer Gemini for the aggregation Stage 2 call due to its large context window.
2. WHEN Gemini is unavailable or rate-limited, THE Document_Intelligence_Service SHALL fall back to Groq for documents with fewer than 30 pages.
3. WHEN both Gemini and Groq are unavailable, THE Document_Intelligence_Service SHALL fall back to Mistral.
4. WHEN a 429 rate-limit response is received during any chunk call, THE Document_Intelligence_Service SHALL wait for the number of seconds reported by `key_manager.seconds_until_available()` before retrying.

---

### Requirement 4: Chat with Document — Session and RAG Pipeline

**User Story:** As a student, I want to ask free-form questions about a PDF and get grounded answers that cite specific pages, so that I can verify the information and navigate to the source.

#### Acceptance Criteria

1. WHEN a user sends a message via `POST /api/chat/stream`, THE Chat_Service SHALL retrieve all page texts for the document from the `pages` table.
2. THE Chat_Service SHALL use the Embedding_Service to compute or retrieve a vector embedding for each page and for the user's query.
3. THE Chat_Service SHALL perform cosine similarity search and select at most `top_k` pages (default 4) as context for the LLM prompt.
4. THE Chat_Service SHALL construct a prompt containing: the document filename as context label, the full text of the top_k retrieved pages, the last 4 turns of conversation history, and the current user message.
5. THE Chat_Service SHALL include the page numbers of the injected context pages in the SSE response as a `citations` event.
6. WHEN a chat session does not yet exist for a document, THE Chat_Service SHALL create a new row in the `chat_sessions` table upon the first message.
7. THE Chat_Service SHALL persist each user message and assistant response as rows in the `chat_messages` table with the associated `session_id`.

### Requirement 5: Chat with Document — Streaming Delivery

**User Story:** As a student, I want chat responses to appear token-by-token, so that I do not have to wait for the full response before reading it.

#### Acceptance Criteria

1. THE Chat_Service SHALL stream responses to the frontend using Server-Sent Events with individual SSE events for each token chunk.
2. WHEN the SSE stream ends normally, THE Chat_Service SHALL emit a final `{"type": "done"}` SSE event.
3. IF all configured API keys for all providers are rate-limited at the time of a chat request, THEN THE Chat_Service SHALL immediately emit a `{"type": "error", "message": "Rate limited. Retry in Xs"}` SSE event with the number of seconds until a key becomes available, and close the stream without making an LLM call.
4. THE Chat_Service SHALL prefer Groq as the chat provider due to its higher RPM (30 RPM), falling back to Gemini then Mistral.

### Requirement 6: Chat with Document — Embedding Caching

**User Story:** As a user, I want page embeddings to be cached in the database, so that opening the chat panel a second time does not re-compute embeddings from scratch.

#### Acceptance Criteria

1. WHEN page embeddings for a document do not exist in the `page_embeddings` table, THE Embedding_Service SHALL compute them and INSERT them before answering the first chat message.
2. WHEN page embeddings already exist in the `page_embeddings` table for a document and the same embedding model, THE Embedding_Service SHALL use the cached embeddings without recomputing.
3. WHERE a HuggingFace token is configured, THE Embedding_Service SHALL use `sentence-transformers/all-MiniLM-L6-v2` via the HuggingFace Inference API to compute embeddings.
4. WHERE no HuggingFace token is configured, THE Embedding_Service SHALL use a local TF-IDF bag-of-words approach to compute similarity scores without any external API call.

---

### Requirement 7: Flashcard Generation — Extraction

**User Story:** As a student, I want to generate flashcards from my document notes without any additional AI calls, so that I can start studying immediately after notes are generated.

#### Acceptance Criteria

1. WHEN `POST /api/flashcards/generate/{doc_id}` is called, THE Flashcard_Service SHALL read the `practice_questions` array from every cached `NoteData` row in the `notes` table for that document.
2. THE Flashcard_Service SHALL create one row in the `flashcards` table for each practice question, with `front` set to the question text and `back` set to the answer text.
3. THE Flashcard_Service SHALL set the `difficulty` column from the `difficulty` field of each practice question (`basic`, `intermediate`, or `advanced`).
4. WHEN a flashcard with the same `document_id`, `page_number`, and `front` already exists, THE Flashcard_Service SHALL skip that card without duplicating it.
5. THE Flashcard_Service SHALL return a response containing the count of newly generated cards, skipped duplicates, and total cards.

### Requirement 8: Flashcard Export

**User Story:** As a student, I want to export my flashcards to Anki and CSV formats, so that I can use them in external study tools.

#### Acceptance Criteria

1. WHEN `GET /api/flashcards/export/{doc_id}?format=apkg` is called, THE Flashcard_Service SHALL return a valid `.apkg` file containing a `collection.anki2` SQLite database and a `media` manifest conforming to the Anki package specification.
2. WHEN `GET /api/flashcards/export/{doc_id}?format=csv` is called, THE Flashcard_Service SHALL return a UTF-8 CSV file with columns `front`, `back`, `difficulty`, and `tags`, where `tags` contains the document filename.
3. THE Flashcard_Service SHALL set the HTTP `Content-Disposition` header to `attachment; filename="flashcards-{doc_id}.{ext}"` for both export formats.

### Requirement 9: Flashcard Spaced Repetition

**User Story:** As a student, I want flashcard reviews to use SM-2 scheduling, so that I see difficult cards more often and cards I know well less often.

#### Acceptance Criteria

1. WHEN `POST /api/flashcards/review` is called with a `card_id` and a `quality` integer between 0 and 5, THE Flashcard_Service SHALL update the card's SM-2 state (`sm2_n`, `sm2_easiness`, `sm2_interval`, `sm2_next_review`) according to the SM-2 algorithm.
2. WHEN `quality` is less than 3, THE Flashcard_Service SHALL reset `sm2_n` to 0 and set `sm2_interval` to 1 day.
3. WHEN `quality` is 3 or greater and `sm2_n` is 0, THE Flashcard_Service SHALL set `sm2_interval` to 1 day.
4. WHEN `quality` is 3 or greater and `sm2_n` is 1, THE Flashcard_Service SHALL set `sm2_interval` to 6 days.
5. WHEN `quality` is 3 or greater and `sm2_n` is greater than 1, THE Flashcard_Service SHALL set `sm2_interval` to `round(previous_interval × easiness)`.
6. THE Flashcard_Service SHALL clamp `sm2_easiness` to a minimum of 1.3 after each update.
7. WHEN `GET /api/flashcards/{doc_id}?due_only=true` is called, THE Flashcard_Service SHALL return only cards where `sm2_next_review` is on or before today's date.

---

### Requirement 10: Highlight & Explain — Text Layer

**User Story:** As a student, I want to select text directly on the PDF page image and receive an instant AI explanation, so that I can clarify confusing passages without switching context.

#### Acceptance Criteria

1. THE System SHALL render a transparent text overlay `<div>` positioned absolutely over each PDF page image, containing the page text from the `pages` table.
2. THE System SHALL enable CSS `user-select: text` on the text overlay so that text selection is possible by the user.
3. WHEN the user releases a mouse selection on the text overlay and the selected text is non-empty, THE System SHALL display a floating action menu near the selection with four options: Explain, Define, Simplify, and Example.

### Requirement 11: Highlight & Explain — Explanation API

**User Story:** As a student, I want each explanation action to return a focused, concise response in under 2 seconds, so that the experience feels interactive.

#### Acceptance Criteria

1. WHEN `POST /api/explain` is called with an `action` of `explain`, THE Explain_Service SHALL return a response explaining the selected text in 2 to 3 sentences.
2. WHEN `POST /api/explain` is called with an `action` of `define`, THE Explain_Service SHALL return a definition of the selected term as used in the surrounding context.
3. WHEN `POST /api/explain` is called with an `action` of `simplify`, THE Explain_Service SHALL return a plain-language restatement of the selected text suitable for a secondary school student.
4. WHEN `POST /api/explain` is called with an `action` of `example`, THE Explain_Service SHALL return one concrete real-world example that illustrates the selected concept.
5. THE Explain_Service SHALL limit the LLM input prompt to at most 200 tokens for each action.
6. THE Explain_Service SHALL prefer Groq as the provider for explain requests due to its 30 RPM limit enabling near-interactive latency.
7. WHEN the selected text exceeds 500 characters, THE Explain_Service SHALL truncate it to 500 characters before sending to the LLM.

---

### Requirement 12: Multi-Document Synthesis — Prompt Construction

**User Story:** As a researcher, I want to ask a single question that spans multiple uploaded PDFs and receive a synthesised answer with per-document citations, so that I can compare content across my study materials.

#### Acceptance Criteria

1. WHEN `POST /api/synthesise` is called, THE Synthesis_Service SHALL accept a `document_ids` array containing between 2 and 10 document IDs.
2. THE Synthesis_Service SHALL retrieve only the `tldr` field (approximately 50 tokens per page) and the top 10 `key_concepts` terms from each document's cached notes to construct the synthesis prompt.
3. THE Synthesis_Service SHALL label each document's section in the prompt with its filename so the LLM can attribute responses correctly.
4. THE Synthesis_Service SHALL instruct the LLM to use `[Doc N, Page M]` citation format in its response.
5. WHEN the total synthesis prompt exceeds 8,000 tokens, THE Synthesis_Service SHALL prefer Gemini as the provider; otherwise it SHALL prefer Groq.
6. WHEN any document in `document_ids` has no cached notes, THE Synthesis_Service SHALL return an HTTP 400 error identifying the unprocessed documents by filename.

### Requirement 13: Multi-Document Synthesis — Response

**User Story:** As a researcher, I want the synthesis response to include structured citations so I can navigate directly to the source pages.

#### Acceptance Criteria

1. THE Synthesis_Service SHALL return an `answer` field containing the synthesised prose response.
2. THE Synthesis_Service SHALL return a `citations` array where each entry contains `document_id`, `filename`, `pages` (array of page numbers), and `excerpt` (a brief quoted phrase from that document).
3. THE Synthesis_Service SHALL include the `model` and `provider` fields in the response to indicate which LLM was used.

---

### Requirement 14: API Key Management — Storage and Security

**User Story:** As a user, I want to add and remove API keys through the browser UI without editing `.env`, so that key management is accessible to non-technical users.

#### Acceptance Criteria

1. THE Key_Manager SHALL store all UI-added keys in a `keys.json` file in the backend directory using the schema `{"gemini": [...], "groq": [...], "mistral": [...], "huggingface": "...", "pollinations": "..."}`.
2. WHEN `GET /api/keys/status` is called, THE Key_Manager SHALL return key status information where the `masked` field for each key contains only the first 4 and last 4 characters of the key, with all middle characters replaced by ellipsis.
3. THE Key_Manager SHALL never include the full value of any API key in any HTTP response body.
4. THE `keys.json` file SHALL be listed in `.gitignore` to prevent accidental version control exposure.

### Requirement 15: API Key Management — Runtime Reload

**User Story:** As a user, I want newly added keys to take effect immediately without restarting the server, so that I can start using them right away.

#### Acceptance Criteria

1. WHEN `POST /api/keys` successfully writes a new key to `keys.json`, THE Key_Manager SHALL call `key_manager.rebuild_provider()` to update the in-memory `KeyManager` with the new key without a server restart.
2. WHEN `DELETE /api/keys/{provider}/{index}` is called, THE Key_Manager SHALL remove the key from `keys.json` and call `key_manager.rebuild_provider()` to remove it from the in-memory `KeyManager`.
3. WHEN rebuilding a provider's keys, THE Key_Manager SHALL preserve the existing `TokenBucket` state for keys that were not removed, so that their rate-limit counters are not reset.

### Requirement 16: API Key Management — Validation

**User Story:** As a user, I want to test an API key before saving it, so that I know whether it is valid without burning generation quota.

#### Acceptance Criteria

1. WHEN `POST /api/keys/probe` is called with a Gemini key, THE Key_Manager SHALL validate it by calling the `GET /v1beta/models` endpoint with that key, which does not consume generation quota.
2. WHEN `POST /api/keys/probe` is called with a Groq key, THE Key_Manager SHALL validate it by calling the `GET /openai/v1/models` endpoint with that key.
3. WHEN `POST /api/keys/probe` is called with a Mistral key, THE Key_Manager SHALL validate it by calling the `GET /v1/models` endpoint with that key.
4. WHEN `POST /api/keys/probe` is called with a HuggingFace token, THE Key_Manager SHALL validate it by calling `GET https://huggingface.co/api/whoami`.
5. ALL probe requests SHALL time out after 5 seconds.
6. THE Key_Manager SHALL return `valid: true`, a descriptive `message`, and the observed `latency_ms` in the probe response.

---

### Requirement 17: Background Batch Processing — Queue Persistence

**User Story:** As a user, I want queued PDF processing jobs to survive a server restart, so that I can queue work overnight without worrying about data loss.

#### Acceptance Criteria

1. THE Batch_Worker SHALL store all job state in the `batch_jobs` SQLite table with columns for `status`, `total_pages`, `completed_pages`, `error_message`, `started_at`, and `completed_at`.
2. WHEN the FastAPI server starts, THE Batch_Worker SHALL query the `batch_jobs` table for any jobs with `status` of `queued` or `processing` and resume them.
3. WHEN a job has `status` of `processing` and `completed_pages` is less than `total_pages` at server start, THE Batch_Worker SHALL resume from the first page that does not yet have a cached note in the `notes` table.

### Requirement 18: Background Batch Processing — Rate-Limit Awareness

**User Story:** As a user, I want batch processing to respect free-tier rate limits automatically, so that my API keys are not blocked during interactive use.

#### Acceptance Criteria

1. THE Batch_Worker SHALL call `key_manager.get_key(provider)` before processing each page, using the same round-robin logic as real-time note generation.
2. WHEN `key_manager.get_key(provider)` returns `None` (all keys exhausted), THE Batch_Worker SHALL call `key_manager.seconds_until_available(provider)` and sleep for that duration before retrying.
3. THE Batch_Worker SHALL wait at least 2 seconds between processing consecutive pages to match the existing inter-page gap in real-time generation.
4. THE Batch_Worker SHALL process one page at a time (no parallel page processing) to avoid saturating the rate-limited APIs.

### Requirement 19: Background Batch Processing — Inbox Watcher

**User Story:** As a power user, I want PDFs placed in an `inbox/` folder to be automatically queued for processing, so that I can batch-load documents without using the browser.

#### Acceptance Criteria

1. WHEN the FastAPI server starts and the `INBOX_DIR` environment variable is set, THE Batch_Worker SHALL monitor that directory for new `.pdf` files.
2. WHERE the `watchdog` Python library is installed, THE Batch_Worker SHALL use `watchdog.FileSystemEventHandler` to detect new files in real time.
3. WHERE the `watchdog` library is not installed, THE Batch_Worker SHALL poll the inbox directory every 30 seconds for new `.pdf` files.
4. WHEN a new `.pdf` file is detected in the inbox, THE Batch_Worker SHALL create a new row in `batch_jobs` with `status='queued'` using the default model and provider from the server configuration.

### Requirement 20: Background Batch Processing — Progress WebSocket

**User Story:** As a user, I want to see real-time progress of batch jobs in the browser, so that I know how long processing will take.

#### Acceptance Criteria

1. THE Batch_Worker SHALL broadcast a `{"type": "page_done", "job_id": N, "completed_pages": X, "total_pages": Y}` message to all connected WebSocket clients at `/ws/batch-progress` after each page is processed.
2. THE Batch_Worker SHALL broadcast a `{"type": "job_done", "job_id": N, "filename": "..."}` message when all pages of a job are complete.
3. THE Batch_Worker SHALL broadcast a `{"type": "job_failed", "job_id": N, "error": "..."}` message when a job fails after exhausting retries.
4. WHEN a client connects to `/ws/batch-progress`, THE System SHALL immediately send a `{"type": "snapshot", "jobs": [...]}` message containing the current state of all jobs.

---

### Requirement 21: Streaming Notes Generation — SSE Endpoint

**User Story:** As a student, I want notes to appear word-by-word as they are generated, so that I can start reading immediately rather than waiting for the full response.

#### Acceptance Criteria

1. THE Streaming_Service SHALL expose a `POST /api/generate-page-note/stream` endpoint with the same request body shape as the existing `/api/generate-page-note` endpoint.
2. THE Streaming_Service SHALL stream LLM output tokens to the frontend using Server-Sent Events.
3. THE Streaming_Service SHALL emit `{"type": "field", "field": "title", "value": "..."}` SSE events as each top-level NoteData field is completed.
4. THE Streaming_Service SHALL emit `{"type": "section", "index": N, "section": {...}}` SSE events as each entry in the `sections` array is completed.
5. THE Streaming_Service SHALL emit a `{"type": "final", "notes": NoteData, "meta": {...}}` SSE event once the complete and validated NoteData JSON is assembled.
6. THE Streaming_Service SHALL emit a `[DONE]` SSE event as the final message to signal stream completion.
7. WHEN an error occurs during streaming, THE Streaming_Service SHALL emit `{"type": "error", "message": "..."}` and close the stream.

### Requirement 22: Streaming Notes Generation — Provider Compatibility

**User Story:** As a user, I want streaming to work with Groq and Gemini, and fall back gracefully for providers that do not support streaming, so that all configured providers remain usable.

#### Acceptance Criteria

1. WHERE the selected provider is Groq, THE Streaming_Service SHALL use the `AsyncGroq` SDK with `stream=True` and `response_format={"type": "json_object"}`.
2. WHERE the selected provider is Gemini, THE Streaming_Service SHALL use the `google-genai` SDK `generate_content_stream` method with `response_mime_type="application/json"`.
3. WHERE the selected provider is Mistral, THE Streaming_Service SHALL fall back to non-streaming generation and emit the complete note as a single `{"type": "final", ...}` SSE event.
4. WHERE the selected provider is Ollama, THE Streaming_Service SHALL use Ollama's `stream: true` parameter in the `/api/generate` call and emit tokens as they arrive.

### Requirement 23: Streaming Notes Generation — Persistence and Interruption

**User Story:** As a student, I want partial notes to be saved if my connection drops mid-generation, so that progress is not completely lost.

#### Acceptance Criteria

1. WHEN the SSE connection is dropped before the final event, THE Streaming_Service SHALL detect the disconnection via `Request.is_disconnected()`.
2. IF the accumulated buffer at disconnection time contains at least both a `title` and a `summary` field, THEN THE Streaming_Service SHALL save the partial NoteData to the `notes` table using the existing `save_note` function.
3. WHEN the stream completes normally and the final NoteData JSON is validated against the NoteData schema, THE Streaming_Service SHALL save it to the `notes` table using `save_note`.
4. THE Streaming_Service SHALL use the same LLM prompt as the non-streaming endpoint, with fields ordered as: `title`, `summary`, `tldr`, `key_concepts`, `sections`, `formulas_and_rules`, `practice_questions`, `common_mistakes`, `brainstorming_ideas`.
