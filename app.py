"""URL shortener that issues links on the m.rta.ae domain.

Run locally:   flask --app app run
Production:    gunicorn -w 4 -b 0.0.0.0:8000 "app:create_app()"
"""

import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Flask, abort, current_app, g, jsonify, redirect, render_template_string, request

ALPHABET = string.ascii_letters + string.digits  # base62
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MAX_URL_LENGTH = 2048
# Paths the app owns; custom aliases may not shadow them.
RESERVED = {"api", "static", "health", "admin", "login", "favicon.ico", "robots.txt"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code        TEXT PRIMARY KEY,
    target_url  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    clicks      INTEGER NOT NULL DEFAULT 0,
    last_click  TEXT
);
"""


def _env_list(name, default):
    raw = os.environ.get(name, default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _now():
    return datetime.now(timezone.utc)


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(
        SHORT_DOMAIN=os.environ.get("SHORT_DOMAIN", "m.rta.ae"),
        SHORT_SCHEME=os.environ.get("SHORT_SCHEME", "https"),
        DATABASE=os.environ.get("DATABASE", "tinyurl.db"),
        CODE_LENGTH=int(os.environ.get("CODE_LENGTH", "7")),
        # Leave API_KEY empty to allow anonymous link creation.
        API_KEY=os.environ.get("API_KEY", ""),
        # Only these domains (and their subdomains) may be shortened.
        # Set to "*" to allow any destination.
        ALLOWED_TARGET_DOMAINS=_env_list("ALLOWED_TARGET_DOMAINS", "rta.ae"),
    )
    if overrides:
        app.config.update(overrides)

    with app.app_context():
        db = _connect(app.config["DATABASE"])
        db.executescript(SCHEMA)
        db.close()

    app.teardown_appcontext(_close_db)
    _register_routes(app)
    return app


# --- database -------------------------------------------------------------

def _connect(path):
    conn = sqlite3.connect(path, detect_types=0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DATABASE"])
    return g.db


def _close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --- validation -----------------------------------------------------------

def validate_target(url, allowed_domains):
    """Return an error message, or None if the URL is acceptable."""
    if not url or not isinstance(url, str):
        return "url is required"
    if len(url) > MAX_URL_LENGTH:
        return f"url exceeds {MAX_URL_LENGTH} characters"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "url must be an absolute http(s) URL"
    if parsed.username or parsed.password:
        return "url must not contain credentials"
    if "*" in allowed_domains:
        return None
    host = parsed.hostname.lower()
    if not any(host == d or host.endswith("." + d) for d in allowed_domains):
        return "destination domain is not allowed"
    return None


def _generate_code(length):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def _short_url(code):
    cfg = current_app.config
    return f"{cfg['SHORT_SCHEME']}://{cfg['SHORT_DOMAIN']}/{code}"


# --- routes ---------------------------------------------------------------

FORM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ domain }} link shortener</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:560px;margin:3rem auto;padding:0 16px;color:#222}
 input,button{font-size:1rem;padding:.6rem;width:100%;box-sizing:border-box;margin:.3rem 0}
 button{background:#c8102e;color:#fff;border:0;border-radius:4px;cursor:pointer}
 #out{margin-top:1rem;word-break:break-all}
</style></head><body>
<h1>{{ domain }}</h1>
<form id="f">
 <input name="url" type="url" placeholder="https://www.rta.ae/..." required>
 <input name="alias" placeholder="Custom alias (optional)">
 {% if need_key %}<input name="key" type="password" placeholder="API key" required>{% endif %}
 <button>Shorten</button>
</form>
<div id="out"></div>
<script>
document.getElementById('f').onsubmit = async e => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  const headers = {'Content-Type': 'application/json'};
  if (d.key) headers['X-API-Key'] = d.key;
  const r = await fetch('/api/shorten', {method: 'POST', headers,
    body: JSON.stringify({url: d.url, alias: d.alias || undefined})});
  const j = await r.json();
  const out = document.getElementById('out');
  out.textContent = '';
  if (r.ok) { const a = document.createElement('a'); a.href = a.textContent = j.short_url; out.append(a); }
  else out.textContent = j.error;
};
</script></body></html>"""


def _register_routes(app):

    def require_key():
        key = app.config["API_KEY"]
        supplied = request.headers.get("X-API-Key", "")
        if key and not secrets.compare_digest(supplied, key):
            abort(401)

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(410)
    def json_error(err):
        return jsonify(error=err.description), err.code

    @app.get("/")
    def index():
        return render_template_string(
            FORM_HTML, domain=app.config["SHORT_DOMAIN"], need_key=bool(app.config["API_KEY"])
        )

    @app.get("/health")
    def health():
        get_db().execute("SELECT 1")
        return jsonify(status="ok")

    @app.post("/api/shorten")
    def shorten():
        require_key()
        data = request.get_json(silent=True) or {}
        url = data.get("url")
        err = validate_target(url, app.config["ALLOWED_TARGET_DOMAINS"])
        if err:
            abort(400, err)

        expires_at = None
        if data.get("expires_in_days") is not None:
            try:
                days = int(data["expires_in_days"])
            except (TypeError, ValueError):
                abort(400, "expires_in_days must be an integer")
            if not 1 <= days <= 3650:
                abort(400, "expires_in_days must be between 1 and 3650")
            expires_at = (_now() + timedelta(days=days)).isoformat()

        db = get_db()
        alias = data.get("alias")
        created = _now().isoformat()

        if alias:
            if not ALIAS_RE.match(alias) or alias.lower() in RESERVED:
                abort(400, "alias must be 3-32 chars of letters, digits, '-' or '_' and not reserved")
            try:
                db.execute(
                    "INSERT INTO links (code, target_url, created_at, expires_at) VALUES (?,?,?,?)",
                    (alias, url, created, expires_at),
                )
            except sqlite3.IntegrityError:
                abort(409, "alias already in use")
            code = alias
        else:
            # Reuse an existing non-expiring code for the same URL.
            if expires_at is None:
                row = db.execute(
                    "SELECT code FROM links WHERE target_url=? AND expires_at IS NULL LIMIT 1",
                    (url,),
                ).fetchone()
                if row:
                    return jsonify(code=row["code"], short_url=_short_url(row["code"]),
                                   url=url, expires_at=None), 200
            for _ in range(10):
                code = _generate_code(app.config["CODE_LENGTH"])
                try:
                    db.execute(
                        "INSERT INTO links (code, target_url, created_at, expires_at) VALUES (?,?,?,?)",
                        (code, url, created, expires_at),
                    )
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                abort(500)

        return jsonify(code=code, short_url=_short_url(code), url=url, expires_at=expires_at), 201

    @app.get("/api/links/<code>")
    def stats(code):
        require_key()
        row = get_db().execute("SELECT * FROM links WHERE code=?", (code,)).fetchone()
        if row is None:
            abort(404, "not found")
        return jsonify(
            code=row["code"], short_url=_short_url(row["code"]), url=row["target_url"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            clicks=row["clicks"], last_click=row["last_click"],
        )

    @app.delete("/api/links/<code>")
    def delete(code):
        require_key()
        cur = get_db().execute("DELETE FROM links WHERE code=?", (code,))
        if cur.rowcount == 0:
            abort(404, "not found")
        return "", 204

    @app.get("/<code>")
    def follow(code):
        db = get_db()
        row = db.execute(
            "SELECT target_url, expires_at FROM links WHERE code=?", (code,)
        ).fetchone()
        if row is None:
            abort(404, "short link not found")
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < _now():
            abort(410, "short link has expired")
        db.execute(
            "UPDATE links SET clicks = clicks + 1, last_click = ? WHERE code = ?",
            (_now().isoformat(), code),
        )
        # 302 so browsers don't cache permanently and clicks keep counting.
        return redirect(row["target_url"], code=302)


if __name__ == "__main__":
    create_app().run(debug=True)
