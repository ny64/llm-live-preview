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
import sqlite3
import subprocess
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
_current_model = "claude-opus-4.7"
_model_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Injected HTML/CSS/JS
# ---------------------------------------------------------------------------

_MATHJAX = '<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js" async></script>'

_EXTRA_CSS = """\
<style>
body {
  max-width: 860px; margin: 2rem auto; padding: 0 1.5rem 160px;
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
  <div id="llm-bar-right">
    <div id="llm-bar-top">
      <select id="llm-model-select" title="Model"></select>
      <button id="llm-history-btn" title="Conversation history">&#128203;</button>
      <button id="llm-new-btn" title="Start a new conversation">New</button>
      <button id="llm-send-btn">Send</button>
    </div>
    <div id="llm-bar-bottom">
      <span id="llm-status"></span>
    </div>
  </div>
</div>

<!-- Conversations panel -->
<div id="llm-conv-panel" style="display:none">
  <div id="llm-conv-header">
    <span>Conversations</span>
    <button id="llm-conv-close" title="Close">&times;</button>
  </div>
  <div id="llm-conv-list"></div>
</div>
<div id="llm-conv-overlay" style="display:none"></div>

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
#llm-bar-right { display: flex; flex-direction: column; gap: 0.3rem; align-items: flex-end; }
#llm-bar-top { display: flex; gap: 0.4rem; align-items: center; }
#llm-bar-bottom { align-self: flex-end; }
#llm-send-btn, #llm-new-btn, #llm-history-btn {
  font-size: 0.85rem; padding: 0.3rem 0.9rem;
  border-radius: 4px; border: 1px solid #aaa; cursor: pointer; white-space: nowrap;
}
#llm-send-btn { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
#llm-send-btn:disabled { opacity: 0.4; cursor: default; }
#llm-input:disabled { opacity: 0.6; }
#llm-new-btn, #llm-history-btn { background: #f5f5f5; }
#llm-new-btn:hover, #llm-history-btn:hover { background: #eaeaea; }
#llm-model-select {
  font-size: 0.82rem; padding: 0.25rem 0.5rem;
  border-radius: 4px; border: 1px solid #ccc; background: #fafafa;
  cursor: pointer; max-width: 220px;
}
#llm-status { font-size: 0.78rem; color: #999; font-family: sans-serif; min-height: 1em; text-align: right; }

/* Conversations panel */
#llm-conv-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.35); z-index: 900;
}
#llm-conv-panel {
  position: fixed; right: 0; top: 0; bottom: 0; width: 360px; max-width: 95vw;
  background: #fff; border-left: 1px solid #ddd; z-index: 901;
  display: flex; flex-direction: column; box-shadow: -4px 0 16px rgba(0,0,0,0.12);
}
#llm-conv-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 0.8rem 1rem; border-bottom: 1px solid #eee;
  font-family: sans-serif; font-weight: 600; font-size: 0.95rem;
}
#llm-conv-close {
  background: none; border: none; font-size: 1.4rem; cursor: pointer;
  color: #666; line-height: 1; padding: 0 0.2rem;
}
#llm-conv-close:hover { color: #111; }
#llm-conv-list { flex: 1; overflow-y: auto; padding: 0.5rem 0; }
.conv-item {
  padding: 0.6rem 1rem; border-bottom: 1px solid #f0f0f0;
  display: flex; gap: 0.5rem; align-items: flex-start;
  cursor: pointer; font-family: sans-serif;
}
.conv-item:hover { background: #f7f7f7; }
.conv-item.active { background: #eef4ff; }
.conv-info { flex: 1; min-width: 0; }
.conv-name {
  font-size: 0.88rem; font-weight: 500; color: #111;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.conv-meta { font-size: 0.75rem; color: #888; margin-top: 0.15rem; }
.conv-del {
  background: none; border: none; color: #bbb; cursor: pointer;
  font-size: 1rem; padding: 0.1rem 0.3rem; border-radius: 3px; flex-shrink: 0;
}
.conv-del:hover { color: #c0392b; background: #fdecea; }
#llm-conv-empty { padding: 1.5rem 1rem; font-family: sans-serif; font-size: 0.88rem; color: #999; }
#llm-conv-loading { padding: 1rem; font-family: sans-serif; font-size: 0.85rem; color: #aaa; }
</style>

<script>
(function () {
  var BUSY_KEY  = 'llm-busy';
  var TEXT_KEY  = 'llm-text';
  var MODEL_KEY = 'llm-model';

  var inp   = document.getElementById('llm-input');
  var send  = document.getElementById('llm-send-btn');
  var nw    = document.getElementById('llm-new-btn');
  var hist  = document.getElementById('llm-history-btn');
  var stat  = document.getElementById('llm-status');
  var msel  = document.getElementById('llm-model-select');
  var panel = document.getElementById('llm-conv-panel');
  var overlay = document.getElementById('llm-conv-overlay');
  var convList = document.getElementById('llm-conv-list');
  var convClose = document.getElementById('llm-conv-close');

  inp.value = sessionStorage.getItem(TEXT_KEY) || '';

  // ---- Model selector -------------------------------------------------------
  fetch('/models').then(function(r){ return r.json(); }).then(function(data) {
    var saved = localStorage.getItem(MODEL_KEY) || data.current;
    data.models.forEach(function(m) {
      var opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.label;
      if (m.id === saved) opt.selected = true;
      msel.appendChild(opt);
    });
    // If saved model not in list, select current
    if (!msel.value) msel.value = data.current;
  }).catch(function(){});

  msel.addEventListener('change', function() {
    localStorage.setItem(MODEL_KEY, msel.value);
    fetch('/set-model', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: msel.value})
    }).catch(function(){});
  });

  // ---- Busy state -----------------------------------------------------------
  function setBusy(on) {
    inp.disabled = send.disabled = on;
    stat.textContent = on ? 'Generating…' : '';
    on ? sessionStorage.setItem(BUSY_KEY, '1') : sessionStorage.removeItem(BUSY_KEY);
  }

  if (sessionStorage.getItem(BUSY_KEY)) setBusy(true);

  // ---- SSE ------------------------------------------------------------------
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

  // ---- Send -----------------------------------------------------------------
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

  // ---- New conversation -----------------------------------------------------
  nw.addEventListener('click', function () {
    fetch('/reset', {method: 'POST'});
    sessionStorage.removeItem(BUSY_KEY);
    sessionStorage.removeItem(TEXT_KEY);
    setBusy(false);
    inp.value = '';
  });

  // ---- Conversations panel --------------------------------------------------
  function openPanel() {
    panel.style.display = 'flex';
    overlay.style.display = 'block';
    loadConversations();
  }
  function closePanel() {
    panel.style.display = 'none';
    overlay.style.display = 'none';
  }

  hist.addEventListener('click', openPanel);
  overlay.addEventListener('click', closePanel);
  convClose.addEventListener('click', closePanel);

  function fmtDate(iso) {
    var d = new Date(iso);
    return d.toLocaleDateString(undefined, {month:'short', day:'numeric'})
      + ' ' + d.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'});
  }

  function loadConversations() {
    convList.innerHTML = '<div id="llm-conv-loading">Loading…</div>';
    fetch('/conversations').then(function(r){ return r.json(); }).then(function(convs) {
      convList.innerHTML = '';
      if (!convs.length) {
        convList.innerHTML = '<div id="llm-conv-empty">No conversations yet.</div>';
        return;
      }
      fetch('/current-conversation').then(function(r){ return r.json(); }).then(function(cur) {
        convs.forEach(function(c) {
          var item = document.createElement('div');
          item.className = 'conv-item' + (c.id === cur.id ? ' active' : '');

          var info = document.createElement('div');
          info.className = 'conv-info';

          var name = document.createElement('div');
          name.className = 'conv-name';
          name.textContent = c.name || c.first_prompt || c.id;

          var meta = document.createElement('div');
          meta.className = 'conv-meta';
          meta.textContent = fmtDate(c.datetime) + ' · ' + (c.model || '').replace('anthropic/', '');

          info.appendChild(name);
          info.appendChild(meta);

          var del = document.createElement('button');
          del.className = 'conv-del';
          del.title = 'Delete conversation';
          del.textContent = '✕';
          del.addEventListener('click', function(e) {
            e.stopPropagation();
            if (!confirm('Delete this conversation?')) return;
            fetch('/delete-conversation', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({id: c.id})
            }).then(function() { loadConversations(); }).catch(function(){});
          });

          item.appendChild(info);
          item.appendChild(del);

          item.addEventListener('click', function() {
            fetch('/load-conversation', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({id: c.id})
            }).then(function(r) {
              if (r.ok) {
                closePanel();
                sessionStorage.removeItem(BUSY_KEY);
                setBusy(false);
              }
            }).catch(function(){});
          });

          convList.appendChild(item);
        });
      }).catch(function() {
        // If no current conversation endpoint, just render without active marker
        convs.forEach(function(c) {
          var item = document.createElement('div');
          item.className = 'conv-item';
          item.innerHTML = '<div class="conv-info"><div class="conv-name">'
            + (c.name || c.first_prompt || c.id).replace(/</g,'&lt;')
            + '</div><div class="conv-meta">' + fmtDate(c.datetime)
            + ' &middot; ' + (c.model||'').replace('anthropic/','')
            + '</div></div>';
          convList.appendChild(item);
        });
      });
    }).catch(function() {
      convList.innerHTML = '<div id="llm-conv-empty">Failed to load conversations.</div>';
    });
  }
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

def _latest_conv_id() -> str | None:
    r = subprocess.run(
        ['llm', 'logs', 'list', '-n', '1', '--json'],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)[0]['conversation_id']
    except Exception:
        return None


def _run_llm(prompt: str) -> None:
    global _running, _conversation_started, _current_conv_id
    llm_conv = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'llm-conv')
    with _model_lock:
        model = _current_model
    args = [llm_conv, '-m', model]
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
        cid = _latest_conv_id()
        if cid:
            with _conv_id_lock:
                _current_conv_id = cid
        _broadcast('done')


# ---------------------------------------------------------------------------
# Helper: get conversations from llm logs
# ---------------------------------------------------------------------------

def _get_conversations(limit: int = 50) -> list[dict]:
    result = subprocess.run(
        ['llm', 'logs', 'list', '-n', str(limit * 5), '--json'],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        entries = json.loads(result.stdout)
    except Exception:
        return []

    seen: dict[str, dict] = {}
    for e in entries:
        cid = e.get('conversation_id')
        if not cid or cid in seen:
            continue
        name = e.get('conversation_name') or ''
        first_prompt = (e.get('prompt') or '')[:80]
        seen[cid] = {
            'id': cid,
            'name': name,
            'first_prompt': first_prompt,
            'model': e.get('conversation_model') or e.get('model') or '',
            'datetime': e.get('datetime_utc', ''),
        }
        if len(seen) >= limit:
            break
    return list(seen.values())


def _get_models() -> list[dict]:
    result = subprocess.run(
        ['llm', 'models', 'list'],
        capture_output=True, text=True
    )
    models = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith('Default:'):
                continue
            # Format: "Provider: model-id (aliases: ...)"
            m = re.match(r'^[^:]+:\s+(\S+)', line)
            if not m:
                continue
            model_id = m.group(1)
            # Extract aliases
            alias_m = re.search(r'\(aliases:\s*([^)]+)\)', line)
            if alias_m:
                aliases = [a.strip() for a in alias_m.group(1).split(',')]
                label = aliases[0]  # use shortest/friendliest alias
            else:
                label = model_id
            models.append({'id': model_id, 'label': label})
    return models


def _load_conversation_to_file(conv_id: str) -> bool:
    """Rewrite _md_file from a past conversation's log."""
    result = subprocess.run(
        ['llm', 'logs', 'list', '--conversation', conv_id, '-n', '0', '--json'],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        entries = json.loads(result.stdout)
    except Exception:
        return False
    lines = ['# Conversation']
    for entry in entries:
        prompt = entry.get('prompt') or ''
        response = entry.get('response') or ''
        lines.append('\n---\n')
        lines.append(f'**You:** {prompt}\n')
        lines.append(f'**Assistant:**\n\n{response}')
    with open(_md_file, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return True


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_current_conv_id: str | None = None
_conv_id_lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def _json_response(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

        elif self.path == "/models":
            models = _get_models()
            with _model_lock:
                current = _current_model
            self._json_response({'models': models, 'current': current})

        elif self.path == "/conversations":
            convs = _get_conversations()
            self._json_response(convs)

        elif self.path == "/current-conversation":
            with _conv_id_lock:
                cid = _current_conv_id
            self._json_response({'id': cid})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        global _running, _conversation_started, _current_model, _current_conv_id

        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)

        if self.path == "/send":
            try:
                prompt = json.loads(raw).get('prompt', '').strip()
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
            with _conv_id_lock:
                _current_conv_id = None
            with open(_md_file, 'w') as f:
                f.write('# Conversation\n\n')
            self.send_response(200)
            self.end_headers()

        elif self.path == "/set-model":
            try:
                model = json.loads(raw).get('model', '').strip()
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            if model:
                with _model_lock:
                    _current_model = model
            self.send_response(200)
            self.end_headers()

        elif self.path == "/load-conversation":
            try:
                conv_id = json.loads(raw).get('id', '').strip()
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            if not conv_id:
                self.send_response(400)
                self.end_headers()
                return
            ok = _load_conversation_to_file(conv_id)
            if ok:
                with _conv_lock:
                    _conversation_started = True
                with _conv_id_lock:
                    _current_conv_id = conv_id
                self.send_response(200)
            else:
                self.send_response(500)
            self.end_headers()

        elif self.path == "/delete-conversation":
            try:
                conv_id = json.loads(raw).get('id', '').strip()
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            try:
                db_path = subprocess.run(
                    ['llm', 'logs', 'path'], capture_output=True, text=True
                ).stdout.strip()
                con = sqlite3.connect(db_path)
                con.execute('DELETE FROM responses WHERE conversation_id = ?', (conv_id,))
                con.execute('DELETE FROM conversations WHERE id = ?', (conv_id,))
                con.commit()
                con.close()
                with _conv_id_lock:
                    if _current_conv_id == conv_id:
                        _current_conv_id = None
                self.send_response(200)
            except Exception:
                self.send_response(500)
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
