#!/usr/bin/env python3
"""
llm-live-preview: Auto-refreshing markdown+LaTeX preview.

Usage:
    python3 preview.py [file.md] [--port 8765] [--no-browser]

The server watches the file, rerenders it with pandoc+MathJax on every
save, and tells the browser to reload via Server-Sent Events.
"""

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# SSE broadcast — one queue per connected browser tab
# ---------------------------------------------------------------------------
_clients: set[queue.Queue] = set()
_clients_lock = threading.Lock()


def _broadcast(msg: str) -> None:
    with _clients_lock:
        dead: set[queue.Queue] = set()
        for q in _clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.add(q)
        _clients.difference_update(dead)


# ---------------------------------------------------------------------------
# Rendered HTML state (updated by watcher thread, read by HTTP thread)
# ---------------------------------------------------------------------------
_html = ""
_html_lock = threading.Lock()

_RELOAD_JS = """\
<script>
(function () {
  var es = new EventSource('/events');
  es.onmessage = function (e) { if (e.data === 'reload') location.reload(); };
  es.onerror   = function ()  { setTimeout(function () { location.reload(); }, 2000); };
})();
</script>"""

_MATHJAX = '<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js" async></script>'

_EXTRA_CSS = """\
<style>
body {
  max-width: 860px; margin: 2rem auto; padding: 0 1.5rem;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.25rem; line-height: 1.5; color: #1a1a1a;
}
h1, h2, h3, h4 { font-family: Georgia, sans-serif; color: #111; }
h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.2em; margin-top: 2em; }
code { font-family: "IBM Plex Mono", "Cascadia Code", monospace;
       font-size: 0.88em; background: #f3f3f3;
       border-radius: 3px; padding: 0.1em 0.35em; }
pre  { background: #f3f3f3; border-radius: 5px;
       padding: 0.9em 1.1em; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #bbb; margin-left: 0;
             padding-left: 1em; color: #555; }
</style>"""

_EMPTY_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>llm-live-preview</title>{css}</head>
<body><p style="color:#888;font-family:sans-serif">
Waiting for content — write to the file to begin.</p>
{js}</body></html>""".format(css=_EXTRA_CSS, js=_RELOAD_JS)


def _render(md_file: str) -> str:
    """Run pandoc, inject CSS + reload script, return full HTML."""
    result = subprocess.run(
        ["pandoc", md_file, "--standalone", "--mathjax", "-f", "markdown"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (
            "<html><body><pre style='color:red'>pandoc error:\n"
            + result.stderr
            + "</pre></body></html>"
        )
    html = result.stdout
    # Strip pandoc's injected MathJax scripts (local v2, broken without config)
    html = re.sub(r'<script[^>]*polyfill\.io[^>]*></script>\s*', '', html)
    html = re.sub(r'<script[^>]*MathJax\.js[^>]*>\s*</script>\s*', '', html)
    html = html.replace("</head>", _MATHJAX + "\n" + _EXTRA_CSS + "\n</head>", 1)
    html = html.replace("</body>", _RELOAD_JS + "\n</body>", 1)
    return html


# ---------------------------------------------------------------------------
# File watcher (polling — no extra deps)
# ---------------------------------------------------------------------------

def _watch(md_file: str, interval: float = 0.8) -> None:
    global _html
    last_mtime: float | None = None
    while True:
        try:
            mtime = os.path.getmtime(md_file)
            if mtime != last_mtime:
                last_mtime = mtime
                html = _render(md_file)
                with _html_lock:
                    _html = html
                _broadcast("reload")
        except FileNotFoundError:
            pass
        time.sleep(interval)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            with _html_lock:
                body = _html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue[str] = queue.Queue(maxsize=10)
            with _clients_lock:
                _clients.add(q)
            try:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(f"data: {msg}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Keepalive comment so the connection stays open
                        self.wfile.write(b": ka\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _clients_lock:
                    _clients.discard(q)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_) -> None:
        pass  # silence access log


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Live pandoc+MathJax preview for markdown files"
    )
    ap.add_argument(
        "file",
        nargs="?",
        default="conversation.md",
        help="Markdown file to watch (default: conversation.md)",
    )
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="Don't open browser")
    args = ap.parse_args()

    md_file = args.file

    if not os.path.exists(md_file):
        #Path(md_file).write_text("# Conversation\n\n")
        print(f"Created {md_file}")

    global _html
    _html = _render(md_file)

    watcher = threading.Thread(target=_watch, args=(md_file,), daemon=True)
    watcher.start()

    server = ThreadingHTTPServer(("localhost", args.port), _Handler)
    url = f"http://localhost:{args.port}"
    print(f"Watching : {md_file}")
    print(f"Preview  : {url}")
    print("Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
