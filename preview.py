#!/usr/bin/env python3
"""
llm-live-preview: Auto-refreshing markdown+LaTeX preview with input bar.

Usage:
    python3 preview.py [file.md] [--port 8765] [--no-browser]

The server watches the file, rerenders it with pandoc+MathJax on every
save, and tells the browser to reload via Server-Sent Events.
Type prompts in the bottom bar to continue the conversation.
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------
_md_file = "conversation.md"
_running = False
_running_lock = threading.Lock()
_conversation_started = False
_conv_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Injected HTML/CSS/JS
# ---------------------------------------------------------------------------

_MATHJAX = '<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js" async></script>'

_EXTRA_CSS = """\
<style>
body {
  max-width: 860px; margin: 2rem auto; padding: 0 1.5rem 140px;
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

_INPUT_BAR = """\
<div id="llm-bar">
  <textarea id="llm-input" placeholder="Message… (Ctrl+Enter to send)" rows="3"></textarea>
  <div id="llm-bar-buttons">
    <span id="llm-status"></span>
    <button id="llm-new-btn" title="Start a new conversation">New</button>
    <button id="llm-send-btn">Send</button>
  </div>
</div>
<style>
#llm-bar {
  position: fixed; bottom: 0; left: 0; right: 0;
  background: #fff; border-top: 1px solid #ddd;
  padding: 0.6rem 1rem; display: flex; gap: 0.6rem;
  align-items: flex-end; box-shadow: 0 -2px 8px rgba(0,0,0,0.07);
  box-sizing: border-box;
}
#llm-input {
  flex: 1; font-family: Georgia, serif; font-size: 1rem;
  border: 1px solid #ccc; border-radius: 4px;
  padding: 0.45rem 0.6rem; resize: vertical;
  min-height: 2.4rem; line-height: 1.4;
}
#llm-bar-buttons { display: flex; flex-direction: column; gap: 0.3rem; align-items: flex-end; }
#llm-send-btn, #llm-new-btn {
  font-size: 0.85rem; padding: 0.3rem 0.9rem;
  border-radius: 4px; border: 1px solid #aaa; cursor: pointer; white-space: nowrap;
}
#llm-send-btn { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
#llm-send-btn:disabled { opacity: 0.4; cursor: default; }
#llm-input:disabled { opacity: 0.6; }
#llm-new-btn { background: #f5f5f5; }
#llm-new-btn:hover { background: #eaeaea; }
#llm-status { font-size: 0.78rem; color: #999; font-family: sans-serif; min-height: 1em; text-align: right; }
</style>
<script>
(function () {
  var BUSY_KEY = 'llm-busy';
  var TEXT_KEY = 'llm-text';
  var inp  = document.getElementById('llm-input');
  var send = document.getElementById('llm-send-btn');
  var nw   = document.getElementById('llm-new-btn');
  var stat = document.getElementById('llm-status');

  inp.value = sessionStorage.getItem(TEXT_KEY) || '';

  function setBusy(on) {
    inp.disabled = send.disabled = on;
    stat.textContent = on ? 'Generating…' : '';
    on ? sessionStorage.setItem(BUSY_KEY, '1') : sessionStorage.removeItem(BUSY_KEY);
  }

  if (sessionStorage.getItem(BUSY_KEY)) setBusy(true);

  var es = new EventSource('/events');
  es.onmessage = function (e) {
    if (e.data === 'reload') {
      sessionStorage.setItem(TEXT_KEY, inp.value);
      location.reload();
    } else if (e.data === 'done') {
      setBusy(false);
      sessionStorage.removeItem(TEXT_KEY);
    }
  };
  es.onerror = function () { setTimeout(function () { location.reload(); }, 2000); };

  function doSend() {
    var text = inp.value.trim();
    if (!text || inp.disabled) return;
    setBusy(true);
    sessionStorage.removeItem(TEXT_KEY);
    fetch('/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: text})
    }).then(function (r) {
      if (r.status === 409) { setBusy(false); stat.textContent = 'Busy…'; }
    }).catch(function () { setBusy(false); stat.textContent = 'Error'; });
  }

  send.addEventListener('click', doSend);
  inp.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSend(); }
  });
  nw.addEventListener('click', function () {
    fetch('/reset', {method: 'POST'});
    sessionStorage.removeItem(BUSY_KEY);
    sessionStorage.removeItem(TEXT_KEY);
    setBusy(false);
    inp.value = '';
  });
})();
</script>"""

_EMPTY_HTML = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    '<title>llm-live-preview</title>'
    + _EXTRA_CSS
    + '</head><body><p style="color:#888;font-family:sans-serif">'
    'Waiting for content — type below to begin.</p>'
    + _INPUT_BAR
    + '</body></html>'
)


def _render(md_file: str) -> str:
    """Run pandoc, inject CSS + input bar, return full HTML."""
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
    html = re.sub(r'<script[^>]*polyfill\.io[^>]*></script>\s*', '', html)
    html = re.sub(r'<script[^>]*MathJax\.js[^>]*>\s*</script>\s*', '', html)
    html = html.replace("</head>", _MATHJAX + "\n" + _EXTRA_CSS + "\n</head>", 1)
    html = html.replace("</body>", _INPUT_BAR + "\n</body>", 1)
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
# llm-conv runner
# ---------------------------------------------------------------------------

def _run_llm(prompt: str) -> None:
    global _running, _conversation_started
    llm_conv = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'llm-conv')
    args = [llm_conv]
    with _conv_lock:
        if _conversation_started:
            args.append('-c')
    args.append(prompt)

    env = os.environ.copy()
    env['LLM_PREVIEW_FILE'] = os.path.abspath(_md_file)

    try:
        subprocess.run(args, env=env)
    finally:
        with _conv_lock:
            _conversation_started = True
        with _running_lock:
            _running = False
        _broadcast('done')


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

    def do_POST(self) -> None:
        global _running, _conversation_started

        if self.path == "/send":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                prompt = json.loads(body).get('prompt', '').strip()
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

            if not prompt:
                self.send_response(400)
                self.end_headers()
                return

            with _running_lock:
                if _running:
                    self.send_response(409)
                    self.end_headers()
                    return
                _running = True

            self.send_response(200)
            self.end_headers()
            threading.Thread(target=_run_llm, args=(prompt,), daemon=True).start()

        elif self.path == "/reset":
            with _conv_lock:
                _conversation_started = False
            with open(_md_file, 'w') as f:
                f.write('# Conversation\n\n')
            self.send_response(200)
            self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_) -> None:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _html, _md_file

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

    _md_file = args.file

    if os.path.exists(_md_file):
        _html = _render(_md_file)
    else:
        _html = _EMPTY_HTML

    watcher = threading.Thread(target=_watch, args=(_md_file,), daemon=True)
    watcher.start()

    server = ThreadingHTTPServer(("localhost", args.port), _Handler)
    url = f"http://localhost:{args.port}"
    print(f"Watching : {_md_file}")
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
