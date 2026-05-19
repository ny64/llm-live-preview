# llm-pdf

A companion script for the [`llm`](https://github.com/simonw/llm) CLI that converts conversations to PDF, with live-reload watching and database editing.

## Requirements

- [`llm`](https://github.com/simonw/llm) installed and configured
- `pandoc` with `xelatex` (for PDF rendering)
- Fonts: TeX Gyre Pagella (body), TeX Gyre Cursor (mono)

## Usage

```
./llm-pdf [conversation_id] [options]
```

### List recent conversations

```bash
./llm-pdf
./llm-pdf --list
```

Prints a table of recent conversations with their ID, turn count, and name.

### Convert a conversation to PDF

```bash
./llm-pdf <conversation_id>
```

Fetches all turns from the `llm` database, converts LaTeX delimiters to pandoc-compatible format, renders to PDF via xelatex, and opens it.

### Inspect turns before converting

```bash
./llm-pdf <conversation_id> --list-turns
```

Prints each turn with its number, timestamp, and a prompt preview — useful for picking turn numbers to exclude or delete.

### Exclude turns from the PDF

```bash
./llm-pdf <conversation_id> -x 2
./llm-pdf <conversation_id> -x 2,4
```

Skips the specified turn(s) in the rendered PDF without touching the database.

### Watch mode (live reload)

```bash
./llm-pdf <conversation_id> --watch
./llm-pdf <conversation_id> --watch --interval 10
```

Builds an initial PDF, opens it, then polls the database every N seconds (default: 5). Whenever a new turn is added to the conversation, the PDF is regenerated in-place. PDF viewers that support live reload (e.g. Evince, Okular) will update automatically. Stop with Ctrl+C.

### Delete turns from the database

```bash
./llm-pdf <conversation_id> -d 3
./llm-pdf <conversation_id> -d 2,4
```

Permanently removes the specified turn(s) from the `llm` sqlite database. Shows a preview of what will be deleted and asks for confirmation. If all turns are removed, the conversation record is deleted as well.

### Custom output path

```bash
./llm-pdf <conversation_id> -o my_notes.pdf
```

### Skip auto-opening the PDF

```bash
./llm-pdf <conversation_id> --no-open
```

## All options

| Flag | Description |
|------|-------------|
| *(no args)* | List recent conversations |
| `-l`, `--list` | List recent conversations |
| `--list-turns` | Show numbered turns without converting |
| `-x`, `--exclude 2,4` | Exclude turn(s) from the PDF |
| `-w`, `--watch` | Rebuild PDF on new turns |
| `--interval N` | Polling interval in seconds for `--watch` (default: 5) |
| `-d`, `--delete 2,4` | Permanently delete turn(s) from the database |
| `-o`, `--output path` | Output PDF path (default: `conversation_<id>.pdf`) |
| `--no-open` | Don't open the PDF after conversion |
