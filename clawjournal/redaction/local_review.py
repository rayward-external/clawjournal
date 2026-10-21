"""Authenticated local review, using the workbench HTTP/auth framework.

The browser can save through the same validators as the CLI. No hosted request,
capture, export, or upload is performed here. The store is chosen at startup,
never from a browser-supplied filesystem path.
"""
from html import escape
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .evaluation import MAX_INPUT, Store, canonical

PREFIX = "/api/redaction-eval/"


def _token():
    from ..paths import ensure_api_token
    from ..workbench.index import INDEX_DB
    return ensure_api_token(Path(str(INDEX_DB)).parent)


def _respond(handler, status, value, *, html=False):
    raw = (value if html else canonical(value)).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'")
    handler.end_headers()
    handler.wfile.write(raw)


def _local_request(handler):
    port = handler.server.server_address[1]
    host = handler.headers.get("Host", "")
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
        return False
    origin = handler.headers.get("Origin")
    if origin is not None and origin != "http://" + host:
        return False
    # Browser writes must be same-origin, even with the correct bearer token.
    return handler.command != "POST" or origin == "http://" + host


def _root(handler):
    from ..config import CONFIG_DIR
    return getattr(handler.server, "_redaction_eval_store", CONFIG_DIR / "redaction-eval")


def _page(store, params):
    from . import contributions, conversation_pairs, ongoing_contributions
    kind = params.get("kind", ["home"])[0]
    identifier = params.get("id", [""])[0]
    if kind == "pairs":
        # Preserve the existing destination when revisiting an enabled grant.
        destination = conversation_pairs.status(store)["destination"] or "https://data.rayward.ai"
        path = conversation_pairs.settings_page(store, destination)
    elif kind == "ongoing":
        path = ongoing_contributions.settings_page(store)
    elif kind == "review":
        path = store.review(identifier)
    elif kind == "snippet":
        path = contributions.review(store, identifier)
    elif kind == "home":
        captures = "".join(f'<li><a href="?kind=review&amp;id={escape(row["id"], quote=True)}">Review capture {escape(row["id"])}</a></li>' for row in store.list())
        contributions._schema(store)
        proposals = "".join(f'<li><a href="?kind=snippet&amp;id={escape(row["id"], quote=True)}">Review snippet {escape(row["id"])}</a></li>' for row in store.conn.execute("SELECT id FROM contribution_proposals WHERE state!='withdrawn'"))
        return '''<!doctype html><html><head><meta charset="utf-8"><title>Local redaction review</title></head>
        <body style="font:17px/1.6 system-ui;max-width:900px;margin:32px auto;padding:24px">
        <h1>Local redaction review</h1><p>Changes are saved on this computer. Nothing is uploaded here.</p>
        <p><a href="?kind=pairs">Original/redacted sharing permission</a></p>
        <p><a href="?kind=ongoing">Snippet contribution preferences</a></p>
        <h2>Captured conversations</h2><ul>''' + captures + "</ul><h2>Prepared snippets</h2><ul>" + proposals + "</ul></body></html>"
    else:
        raise ValueError("Unknown review page")
    return path.read_text()


def _save(store, action, document):
    from . import contributions, conversation_pairs, ongoing_contributions
    if not isinstance(document, dict):
        raise ValueError("Expected a JSON object")
    if action == "pairs-consent":
        return conversation_pairs.enable(store, document)
    if action == "ongoing-consent":
        return ongoing_contributions.import_permission(store, document)
    if action == "snippet-approve":
        return {"proposal_id": contributions.approve(store, document)}
    if action == "snippet-withdraw" and set(document) == {"proposal_id"}:
        contributions.withdraw(store, document["proposal_id"])
        return {"withdrawn": True}
    if action == "corrections":
        count = store.import_corrections(document)
        return {"annotations": count, "revision": store.get(document["capture_id"])["revision"]}
    raise ValueError("Unknown save operation")


def _bridge(html, store):
    from . import conversation_pairs
    token = json.dumps(_token()).replace("<", "\\u003c")
    script = '''<script nonce="redaction-local">
    window.clawjournalLocal = {pairEnabled:PAIR_ENABLED, async save(action, document) {
      const response = await fetch('/api/redaction-eval/' + action, {
        method:'POST', headers:{'Content-Type':'application/json','Authorization':'Bearer ' + TOKEN},
        body:JSON.stringify(document), credentials:'omit', redirect:'error'
      });
      const result = await response.json();
      if (!response.ok || result.saved !== true) throw new Error(result.error || 'Save was not confirmed');
      return result;
    }};
    </script>'''.replace("TOKEN", token).replace("PAIR_ENABLED", json.dumps(conversation_pairs.status(store)["enabled"]))
    html = html.replace("connect-src 'none'", "connect-src 'self'")
    for nonce in ("pair-settings", "contribution-settings", "snippet-review", "local-evaluation"):
        html = html.replace(f"script-src 'nonce-{nonce}'", f"script-src 'nonce-{nonce}' 'nonce-redaction-local'")
    return html.replace("</head>", script + "</head>", 1).replace("<body>", '<body><p><a href="/redaction-eval">Back to local review</a></p>', 1)


def handle(handler):
    """Return whether this request belongs to the local review surface."""
    parsed = urlparse(handler.path)
    shell = parsed.path in {"/redaction-eval", "/redaction-eval/"}
    if not shell and not parsed.path.startswith(PREFIX):
        return False
    if not _local_request(handler):
        _respond(handler, 403, {"error": "Local same-origin access required"})
        return True
    if shell and handler.command == "GET":
        # Like the workbench shell, bootstrap only the bearer token. No original
        # text is exposed without an authenticated API request.
        token = json.dumps(_token()).replace("<", "\\u003c")
        html = '''<!doctype html><meta charset="utf-8"><title>Local redaction review</title>
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-redaction-local' 'nonce-pair-settings' 'nonce-contribution-settings' 'nonce-snippet-review' 'nonce-local-evaluation'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'">
        <p id="loading">Loading local review…</p><script nonce="redaction-local">
        fetch('/api/redaction-eval/page'+location.search,{headers:{Authorization:'Bearer '+TOKEN},credentials:'omit',redirect:'error'})
          .then(async response=>{if(!response.ok)throw new Error('Unable to open this review. Check that the local store and selected item exist.');return response.text();})
          .then(html=>{document.open();document.write(html);document.close();})
          .catch(error=>{document.getElementById('loading').textContent=error.message;});
        </script>'''.replace("TOKEN", token)
        _respond(handler, 200, html, html=True)
        return True
    if not handler._check_api_auth():
        _respond(handler, 401, {"error": "Local authorization required"})
        return True
    store = None
    try:
        store = Store(_root(handler))
        if parsed.path == PREFIX + "page" and handler.command == "GET":
            _respond(handler, 200, _bridge(_page(store, parse_qs(parsed.query)), store), html=True)
        elif handler.command == "POST" and parsed.path != PREFIX + "page":
            if handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Expected application/json")
            if handler.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer encoding is unsupported")
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_INPUT:
                raise ValueError("Invalid request size")
            handler.connection.settimeout(10)
            raw = handler.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request")
            document = json.loads(raw)
            result = _save(store, parsed.path[len(PREFIX):], document)
            _respond(handler, 200, {"saved": True, **result})
        else:
            _respond(handler, 405, {"error": "Unsupported operation"})
    except (ValueError, KeyError, TypeError, RecursionError):
        # Do not echo source text, labels, or arbitrary parser exception strings.
        _respond(handler, 400, {"error": "Save or review rejected. Check the required choices; if the page is stale, reopen it. Your unsaved edits have not been discarded."})
    except (OSError, RuntimeError, sqlite3.Error):
        _respond(handler, 503, {"error": "Local storage is unavailable; save was not confirmed."})
    finally:
        if store is not None:
            store.close()
    return True


def serve(root, port=0):
    """Use the workbench handler/auth without starting discovery or uploads."""
    from http.server import ThreadingHTTPServer
    from ..workbench.daemon import WorkbenchHandler
    store = Store(root)
    store.close()

    class ReviewHandler(WorkbenchHandler):
        def do_GET(self):
            if not handle(self):
                _respond(self, 404, {"error": "Unknown review route"})

        do_POST = do_GET

        def do_OPTIONS(self):
            _respond(self, 405, {"error": "Unsupported operation"})

        do_PATCH = do_OPTIONS
        do_DELETE = do_OPTIONS

    with ThreadingHTTPServer(("127.0.0.1", port), ReviewHandler) as server:
        server._redaction_eval_store = Path(root)
        print(json.dumps({"review_url": f"http://127.0.0.1:{server.server_address[1]}/redaction-eval", "uploads_enabled": False}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
