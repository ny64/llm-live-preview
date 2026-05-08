# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A single-file local web app (`preview.py`) that serves a live-reloading markdown+LaTeX preview and a browser-based chat UI backed by the `llm` CLI. There are no build steps, no dependencies to install beyond `pandoc` and `llm` (with the Anthropic plugin).

## Running

```bash
python3 preview.py                  # watches conversation.md, opens browser
python3 preview.py notes.md --port 9000 --no-browser
```

## Architecture

### preview.py

Single-file Python HTTP server with three concurrent concerns:

- **File watcher thread** — polls the markdown file every 0.8 s; on change, rerenders via `pandoc` and broadcasts `reload` over SSE.
- **LLM runner thread** — spawned per prompt; calls `llm-conv`, then broadcasts `done` when finished. Guards against concurrent runs with `_running` flag.
- **HTTP server** (`ThreadingHTTPServer`) — serves the rendered page, SSE stream, and a REST API.

State shared across threads: `_html` (rendered page), `_running`, `_conversation_started`, `_current_model`, `_current_conv_id` — each guarded by its own `threading.Lock`.

HTML is injected directly into pandoc output: `_EXTRA_CSS` and `_INPUT_BAR` (which contains all browser JS inline) are string-replaced into `</head>` and `</body>`.

### REST endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Rendered HTML page |
| GET | `/events` | SSE stream (`reload`, `done`, keepalive) |
| GET | `/models` | List from `llm models list` + current model |
| GET | `/conversations` | Last 50 conversations from `llm logs list` |
| GET | `/current-conversation` | Active conversation ID |
| GET | `/messages` | Ordered `[{id, prompt}]` for the active conversation |
| POST | `/send` | Submit a prompt (409 if busy) |
| POST | `/reset` | Clear file and start fresh conversation |
| POST | `/set-model` | Change active model |
| POST | `/load-conversation` | Rewrite file from a past conversation's logs |
| POST | `/delete-conversation` | Delete from llm's SQLite DB directly |
| POST | `/truncate-from` | Delete a response and all after it; rewrites the file |

### llm-conv (bash script)

Thin wrapper around the `llm` CLI that handles two modes:
- **Prompt mode** (default): streams output with `tee -a`, then rewrites the file cleanly from `llm logs list --conversation <id>`.
- **Chat mode** (`chat` subcommand): background poller rewrites the file every 2 s while the interactive session runs.

Reads `LLM_PREVIEW_FILE` env var (set by `preview.py`) to know where to write.

### Conversation continuity

`preview.py` tracks whether a conversation has started (`_conversation_started`) to decide whether to pass `-c` (continue) to `llm-conv`. Loading a past conversation via `/load-conversation` sets `_conversation_started = True` and records the conversation ID; `/reset` clears both.

### Delete and truncate implementation note

`llm` has no `logs delete` subcommand. Deletion goes directly to the SQLite database at the path returned by `llm logs path`, operating on the `responses` and `conversations` tables.

`/delete-conversation` removes all responses and the conversation record. `/truncate-from` takes a response ID, deletes it and all later responses in the same conversation (ordered by `datetime_utc`), then rewrites the markdown file from the remaining entries — or if no entries remain, resets state fully (same effect as `/reset`). The file rewrite triggers the watcher, which broadcasts `reload` to the browser.

### Message-level delete buttons

On each page load and on every `done` SSE event, the browser JS calls `injectDeleteButtons()`: it fetches `/messages`, finds every `<p>` containing `<strong>You:</strong>` (the pandoc rendering of `**You:**`), and appends a ✂ button to each. The Nth button corresponds to the Nth entry from `/messages`. Clicking it POSTs to `/truncate-from` with that entry's ID, which causes the page to reload automatically via the file watcher.
