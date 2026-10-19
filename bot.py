#!/usr/bin/env python3
"""
DSS Server — Database-as-a-Service (PostgreSQL/Neon Edition)
سيرفر تخزين ومزامنة لحظية بواجهة تطبيق APK فخمة + مفاتيح API + Self-Ping.
جاهز للنشر على Render مباشرة.
"""
import os
import json
import time
import uuid
import threading
import queue
import secrets
import hashlib
import urllib.request
import psycopg2
import psycopg2.extras
from functools import wraps
from flask import Flask, request, jsonify, Response, g, session

# ==========================================================
# الإعدادات (تم وضع رابط قاعدة البيانات مباشرة هنا)
# ==========================================================
PORT            = int(os.environ.get("PORT", 5000))
HOST            = "0.0.0.0"

# 👇 الرابط الخاص بك تم وضعه هنا مباشرة 👇
DATABASE_URL    = "postgresql://neondb_owner:npg_FEvO3zoHUcQ0@ep-aged-fog-b52d2rjf-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require"

ADMIN_PASS_ENV  = os.environ.get("ADMIN_PASS", "admin123")
SECRET          = os.environ.get("SECRET") or secrets.token_hex(32)
SELF_URL        = (os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
PING_INTERVAL   = int(os.environ.get("PING_INTERVAL", 600))
START_TS        = int(time.time())

app = Flask(__name__)
app.secret_key = SECRET

# 🟢 زيادة الحد الأقصى لحجم الملفات إلى 50 ميجابايت لدعم التطبيقات الكبيرة
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024 

# ==========================================================
# قاعدة البيانات (PostgreSQL - Neon)
# ==========================================================
def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
        g.db.autocommit = True
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            api_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_used INTEGER,
            requests INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            collection TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_docs ON documents(api_key, collection);
        CREATE TABLE IF NOT EXISTS activity (
            id SERIAL PRIMARY KEY,
            api_key TEXT,
            action TEXT,
            collection TEXT,
            detail TEXT,
            ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_act ON activity(api_key, ts);
    """)
    conn.commit()

    # التحقق من وجود كلمة مرور
    cur.execute("SELECT value FROM config WHERE key='admin_pass_hash'")
    row = cur.fetchone()
    if not row:
        salt = secrets.token_hex(16)
        h = hashlib.sha256((salt + ADMIN_PASS_ENV).encode()).hexdigest()
        cur.execute("INSERT INTO config(key, value) VALUES('admin_salt', %s)", (salt,))
        cur.execute("INSERT INTO config(key, value) VALUES('admin_pass_hash', %s)", (h,))
        conn.commit()

    # التحقق من وجود مفتاح تجريبي
    cur.execute("SELECT COUNT(*) FROM api_keys")
    if cur.fetchone()[0] == 0:
        demo_key = "dss_" + secrets.token_urlsafe(28)
        cur.execute(
            "INSERT INTO api_keys(api_key, name, created_at, active) VALUES(%s,%s,%s,1)",
            (demo_key, "Demo Key", int(time.time()))
        )
        conn.commit()
        print("\n" + "=" * 64, flush=True)
        print("  🔑 مفتاح تجريبي جاهز:", flush=True)
        print("     " + demo_key, flush=True)
        print("=" * 64 + "\n", flush=True)
    conn.close()

init_db()

def get_config(key, default=None):
    cur = get_db().cursor()
    cur.execute("SELECT value FROM config WHERE key=%s", (key,))
    row = cur.fetchone()
    return row[0] if row else default

def set_config(key, value):
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO config(key, value) VALUES(%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))
    db.commit()

def hash_pass(p: str) -> str:
    salt = get_config("admin_salt") or ""
    return hashlib.sha256((salt + p).encode()).hexdigest()

def check_pass(p: str) -> bool:
    return hash_pass(p) == get_config("admin_pass_hash")

# ==========================================================
# PubSub لحظي (SSE)
# ==========================================================
_subs_lock = threading.Lock()
_subs = {}
_admin_lock = threading.Lock()
_admin_subs = set()

def push_collection(api_key, collection, event, data):
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with _subs_lock:
        for q in list(_subs.get((api_key, collection), ())):
            try: q.put_nowait(msg)
            except Exception: pass

def push_admin(payload):
    with _admin_lock:
        for q in list(_admin_subs):
            try: q.put_nowait(payload)
            except Exception: pass

def log_activity(api_key, action, collection="", detail=""):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO activity(api_key, action, collection, detail, ts) VALUES(%s,%s,%s,%s,%s)",
        (api_key, action, collection, detail, int(time.time()))
    )
    db.commit()
    push_admin("event: activity\ndata: " + json.dumps({
        "api_key": api_key, "action": action,
        "collection": collection, "detail": detail,
        "ts": int(time.time())
    }, ensure_ascii=False) + "\n\n")

# ==========================================================
# CORS + المصادقة
# ==========================================================
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return Response("", 204)

def verify_api_key(key):
    if not key: return None
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM api_keys WHERE api_key=%s AND active=1", (key,))
    row = cur.fetchone()
    if not row: return None
    cur.execute("UPDATE api_keys SET last_used=%s, requests=requests+1 WHERE api_key=%s", (int(time.time()), key))
    db.commit()
    return row

def require_key(fn):
    @wraps(fn)
    def w(*a, **kw):
        key = (request.headers.get("X-API-Key")
               or request.args.get("key")
               or (request.get_json(silent=True) or {}).get("api_key"))
        if not verify_api_key(key):
            return jsonify({"ok": False, "error": "invalid_api_key"}), 401
        g.api_key = key
        return fn(*a, **kw)
    return w

def require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        if not session.get("admin"):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*a, **kw)
    return w

# ==========================================================
# Keep-Alive (Self-Ping)
# ==========================================================
def keepalive_loop():
    if not SELF_URL:
        print("[keepalive] ⚠ لا يوجد SELF_URL/RENDER_EXTERNAL_URL — معطّل", flush=True)
        return
    ping_url = SELF_URL + "/api/ping"
    time.sleep(45)
    print(f"[keepalive] ✓ يعمل على {ping_url} كل {PING_INTERVAL}ث", flush=True)
    while True:
        try:
            req = urllib.request.Request(ping_url, headers={"User-Agent": "DSS-KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"[keepalive] ✓ {r.status} @ {time.strftime('%H:%M:%S')}", flush=True)
            time.sleep(PING_INTERVAL)
        except Exception as e:
            print(f"[keepalive] ✗ فشل: {e} — إعادة بعد 60ث", flush=True)
            time.sleep(60)

if SELF_URL:
    threading.Thread(target=keepalive_loop, daemon=True).start()

# ==========================================================
# Routes عامة
# ==========================================================
@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "uptime": int(time.time()) - START_TS})

@app.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    if not check_pass((body.get("password") or "")):
        return jsonify({"ok": False, "error": "wrong_password"}), 401
    session["admin"] = True
    return jsonify({"ok": True})

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

# ==========================================================
# API الإدارة
# ==========================================================
@app.route("/api/admin/check")
def admin_check():
    return jsonify({"ok": True, "admin": bool(session.get("admin"))})

@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    db = get_db()
    cur = db.cursor()
    
    cur.execute("SELECT COUNT(*) FROM api_keys WHERE active=1")
    keys = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM api_keys")
    total_keys = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM documents")
    docs = cur.fetchone()[0]
    
    cur.execute("SELECT COALESCE(SUM(requests),0) FROM api_keys")
    reqs = cur.fetchone()[0]
    
    now = int(time.time())
    by_hour = []
    for i in range(23, -1, -1):
        start = now - i * 3600
        cur.execute("SELECT COUNT(*) FROM activity WHERE ts>=%s AND ts<%s", (start, start + 3600))
        c = cur.fetchone()[0]
        by_hour.append({"hour": i, "count": c})
    return jsonify({"ok": True, "keys": keys, "total_keys": total_keys,
                    "documents": docs, "requests": reqs,
                    "uptime": now - START_TS, "by_hour": by_hour})

@app.route("/api/admin/keys", methods=["GET"])
@require_admin
def admin_keys_list():
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM api_keys ORDER BY created_at DESC")
    rows = cur.fetchall()
    return jsonify({"ok": True, "keys": [dict(r) for r in rows]})

@app.route("/api/admin/keys", methods=["POST"])
@require_admin
def admin_keys_create():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip() or "New Key"
    new_key = "dss_" + secrets.token_urlsafe(28)
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO api_keys(api_key, name, created_at, active) VALUES(%s,%s,%s,1)",
               (new_key, name, int(time.time())))
    db.commit()
    log_activity(new_key, "key_created", "", name)
    return jsonify({"ok": True, "key": new_key, "name": name})

@app.route("/api/admin/keys/<key>", methods=["DELETE"])
@require_admin
def admin_keys_delete(key):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM api_keys WHERE api_key=%s", (key,))
    cur.execute("DELETE FROM documents WHERE api_key=%s", (key,))
    db.commit()
    log_activity(key, "key_deleted")
    return jsonify({"ok": True})

@app.route("/api/admin/keys/<key>/toggle", methods=["POST"])
@require_admin
def admin_keys_toggle(key):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT active FROM api_keys WHERE api_key=%s", (key,))
    row = cur.fetchone()
    if not row: return jsonify({"ok": False}), 404
    new_state = 0 if row[0] else 1
    cur.execute("UPDATE api_keys SET active=%s WHERE api_key=%s", (new_state, key))
    db.commit()
    log_activity(key, "key_toggled", "", "active" if new_state else "inactive")
    return jsonify({"ok": True, "active": new_state})

@app.route("/api/admin/documents")
@require_admin
def admin_documents():
    key = request.args.get("key", "")
    collection = request.args.get("collection", "")
    if not key: return jsonify({"ok": False, "error": "key_required"}), 400
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    if collection:
        cur.execute("SELECT * FROM documents WHERE api_key=%s AND collection=%s ORDER BY updated_at DESC LIMIT 500", (key, collection))
    else:
        cur.execute("SELECT * FROM documents WHERE api_key=%s ORDER BY updated_at DESC LIMIT 500", (key,))
    rows = cur.fetchall()
    out = []
    for r in rows:
        try: data = json.loads(r["data"])
        except Exception: data = r["data"]
        out.append({"id": r["id"], "collection": r["collection"], "data": data,
                    "created_at": r["created_at"], "updated_at": r["updated_at"]})
    return jsonify({"ok": True, "documents": out})

@app.route("/api/admin/collections")
@require_admin
def admin_collections():
    key = request.args.get("key", "")
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT DISTINCT collection FROM documents WHERE api_key=%s ORDER BY collection", (key,))
    rows = cur.fetchall()
    return jsonify({"ok": True, "collections": [r[0] for r in rows]})

@app.route("/api/admin/documents/<doc_id>", methods=["DELETE"])
@require_admin
def admin_doc_delete(doc_id):
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
    row = cur.fetchone()
    if not row: return jsonify({"ok": False}), 404
    cur.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
    db.commit()
    push_collection(row["api_key"], row["collection"], "delete", {"id": doc_id})
    log_activity(row["api_key"], "delete", row["collection"], doc_id)
    return jsonify({"ok": True})

@app.route("/api/admin/activity")
@require_admin
def admin_activity():
    limit = min(int(request.args.get("limit", 50)), 300)
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM activity ORDER BY ts DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    return jsonify({"ok": True, "activity": [dict(r) for r in rows]})

@app.route("/api/admin/stream")
@require_admin
def admin_stream():
    def gen():
        q = queue.Queue()
        with _admin_lock: _admin_subs.add(q)
        try:
            yield f"event: hello\ndata: {json.dumps({'ok': True})}\n\n"
            while True:
                try: yield q.get(timeout=20)
                except queue.Empty: yield ": keep-alive\n\n"
        except GeneratorExit: pass
        finally:
            with _admin_lock: _admin_subs.discard(q)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

@app.route("/api/admin/password", methods=["POST"])
@require_admin
def admin_password():
    body = request.get_json(silent=True) or {}
    if not check_pass(body.get("old", "")):
        return jsonify({"ok": False, "error": "wrong_password"}), 401
    if len(body.get("new", "")) < 6:
        return jsonify({"ok": False, "error": "too_short"}), 400
    set_config("admin_pass_hash", hash_pass(body["new"]))
    return jsonify({"ok": True})

@app.route("/api/admin/export")
@require_admin
def admin_export():
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM api_keys")
    keys = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM documents")
    docs = [dict(r) for r in cur.fetchall()]
    payload = {
        "exported_at": int(time.time()),
        "generator": "DSS Server (PostgreSQL)",
        "keys": keys, "documents": docs
    }
    return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                    mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=dss_backup.json"})

@app.route("/api/admin/import", methods=["POST"])
@require_admin
def admin_import():
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("keys"), list):
        return jsonify({"ok": False, "error": "invalid_format"}), 400
    db = get_db()
    cur = db.cursor()
    ik = id_ = 0
    for k in body["keys"]:
        try:
            cur.execute("INSERT INTO api_keys(api_key,name,created_at,last_used,requests,active) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (k["api_key"], k.get("name", ""), k.get("created_at", int(time.time())),
                        k.get("last_used"), k.get("requests", 0), k.get("active", 1)))
            ik += 1
        except Exception: pass
    for d in body.get("documents", []):
        try:
            cur.execute("INSERT INTO documents(id,api_key,collection,data,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (d["id"], d["api_key"], d["collection"], d["data"],
                        d.get("created_at", int(time.time())), d.get("updated_at", int(time.time()))))
            id_ += 1
        except Exception: pass
    db.commit()
    return jsonify({"ok": True, "keys": ik, "documents": id_})

# ==========================================================
# API العملاء
# ==========================================================
@app.route("/api/db", methods=["GET"])
@require_key
def list_collections():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT DISTINCT collection FROM documents WHERE api_key=%s ORDER BY collection", (g.api_key,))
    rows = cur.fetchall()
    return jsonify({"ok": True, "collections": [r[0] for r in rows]})

@app.route("/api/db/<collection>", methods=["POST"])
@require_key
def insert_doc(collection):
    body = request.get_json(silent=True)
    if body is None: return jsonify({"ok": False, "error": "invalid_json"}), 400
    items = body if isinstance(body, list) else body.get("docs") if isinstance(body, dict) and "docs" in body else [body]
    if not isinstance(items, list): return jsonify({"ok": False, "error": "invalid_payload"}), 400

    db = get_db()
    cur = db.cursor()
    now = int(time.time() * 1000)
    created = []
    for item in items:
        if not isinstance(item, dict): continue
        doc_id = str(item.pop("_id", None) or uuid.uuid4().hex[:16])
        
        cur.execute("SELECT id FROM documents WHERE id=%s", (doc_id,))
        existing = cur.fetchone()
        
        if existing:
            cur.execute("UPDATE documents SET data=%s, updated_at=%s WHERE id=%s",
                       (json.dumps(item, ensure_ascii=False), now, doc_id))
            evt = "update"
        else:
            cur.execute("INSERT INTO documents(id,api_key,collection,data,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",
                       (doc_id, g.api_key, collection, json.dumps(item, ensure_ascii=False), now, now))
            evt = "insert"
        created.append({"_id": doc_id, **item})
        push_collection(g.api_key, collection, evt, {"id": doc_id, "data": item})
    db.commit()
    log_activity(g.api_key, "insert", collection, f"{len(created)} doc(s)")
    return jsonify({"ok": True, "count": len(created), "documents": created}), 201

@app.route("/api/db/<collection>", methods=["GET"])
@require_key
def list_docs(collection):
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    limit = min(int(request.args.get("limit", 200)), 1000)
    since = request.args.get("since")
    order = "ASC" if request.args.get("order") == "asc" else "DESC"
    
    sql = "SELECT * FROM documents WHERE api_key=%s AND collection=%s"
    params = [g.api_key, collection]
    if since:
        sql += " AND updated_at > %s"
        params.append(int(since))
    sql += f" ORDER BY updated_at {order} LIMIT %s"
    params.append(limit)
    
    cur.execute(sql, params)
    rows = cur.fetchall()
    out = []
    for r in rows:
        try: data = json.loads(r["data"])
        except Exception: data = r["data"]
        out.append({"_id": r["id"], "data": data,
                    "created_at": r["created_at"], "updated_at": r["updated_at"]})
    return jsonify({"ok": True, "count": len(out), "documents": out})

@app.route("/api/db/<collection>/<doc_id>", methods=["GET"])
@require_key
def get_doc(collection, doc_id):
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM documents WHERE id=%s AND api_key=%s AND collection=%s", (doc_id, g.api_key, collection))
    row = cur.fetchone()
    if not row: return jsonify({"ok": False, "error": "not_found"}), 404
    try: data = json.loads(row["data"])
    except Exception: data = row["data"]
    return jsonify({"_id": row["id"], "data": data,
                    "created_at": row["created_at"], "updated_at": row["updated_at"]})

@app.route("/api/db/<collection>/<doc_id>", methods=["PUT"])
@require_key
def replace_doc(collection, doc_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict): return jsonify({"ok": False, "error": "invalid_body"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM documents WHERE id=%s AND api_key=%s AND collection=%s", (doc_id, g.api_key, collection))
    if not cur.fetchone():
        return jsonify({"ok": False, "error": "not_found"}), 404
    now = int(time.time() * 1000)
    cur.execute("UPDATE documents SET data=%s, updated_at=%s WHERE id=%s",
               (json.dumps(body, ensure_ascii=False), now, doc_id))
    db.commit()
    push_collection(g.api_key, collection, "update", {"id": doc_id, "data": body})
    log_activity(g.api_key, "update", collection, doc_id)
    return jsonify({"ok": True, "_id": doc_id, "data": body})

@app.route("/api/db/<collection>/<doc_id>", methods=["PATCH"])
@require_key
def patch_doc(collection, doc_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict): return jsonify({"ok": False, "error": "invalid_body"}), 400
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT data FROM documents WHERE id=%s AND api_key=%s AND collection=%s", (doc_id, g.api_key, collection))
    row = cur.fetchone()
    if not row: return jsonify({"ok": False, "error": "not_found"}), 404
    try: existing = json.loads(row["data"])
    except Exception: existing = {}
    existing.update(body)
    now = int(time.time() * 1000)
    cur.execute("UPDATE documents SET data=%s, updated_at=%s WHERE id=%s",
               (json.dumps(existing, ensure_ascii=False), now, doc_id))
    db.commit()
    push_collection(g.api_key, collection, "update", {"id": doc_id, "data": existing})
    log_activity(g.api_key, "update", collection, doc_id)
    return jsonify({"ok": True, "_id": doc_id, "data": existing})

@app.route("/api/db/<collection>/<doc_id>", methods=["DELETE"])
@require_key
def delete_doc(collection, doc_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM documents WHERE id=%s AND api_key=%s AND collection=%s", (doc_id, g.api_key, collection))
    if not cur.fetchone():
        return jsonify({"ok": False, "error": "not_found"}), 404
    cur.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
    db.commit()
    push_collection(g.api_key, collection, "delete", {"id": doc_id})
    log_activity(g.api_key, "delete", collection, doc_id)
    return jsonify({"ok": True})

@app.route("/api/db/<collection>/stream")
@require_key
def stream_collection(collection):
    key = g.api_key
    def gen():
        q = queue.Queue()
        with _subs_lock: _subs.setdefault((key, collection), set()).add(q)
        try:
            yield f"event: hello\ndata: {json.dumps({'ok': True, 'collection': collection})}\n\n"
            while True:
                try: yield q.get(timeout=20)
                except queue.Empty: yield ": keep-alive\n\n"
        except GeneratorExit: pass
        finally:
            with _subs_lock:
                s = _subs.get((key, collection), set())
                s.discard(q)
                if not s: _subs.pop((key, collection), None)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

# ==========================================================
# HTML Dashboard (تطبيق APK فخم)
# ==========================================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#070709">
<title>DSS — لوحة التحكم VIP</title>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;500;600;700;800;900&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
/* Reset & Vars */
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; height: 100%; }
body {
  height: 100dvh;
  display: flex; flex-direction: column;
  font-family: 'Cairo', system-ui, sans-serif;
  background: #070709; color: #f3f0e9;
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}
body::before {
  content: ''; position: fixed; inset: 0; z-index: -1; pointer-events: none;
  background: radial-gradient(circle at 80% -10%, rgba(212,175,106,.15), transparent 60%),
              radial-gradient(circle at -10% 20%, rgba(122,90,205,.08), transparent 60%);
}
:root {
  --gold: #d4af6a; --gold-2: #f2dca6; --gold-3: #a67c34;
  --text: #f3f0e9; --muted: #9b968c; --muted-2: #6f6b63;
  --line: rgba(212,175,106,.25); --line-soft: rgba(255,255,255,.08);
  --card: rgba(255,255,255,.04); --ok: #6bd39a; --danger: #e06b6b;
  --r: 20px; --r-sm: 14px;
  --appbar-h: 60px;
  --tabbar-h: 70px;
}
button, input, select, textarea { font: inherit; color: inherit; outline: none; }
button { cursor: pointer; background: none; border: none; }
a { color: inherit; text-decoration: none; }
::selection { background: rgba(212,175,106,.3); color: #fff; }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: rgba(212,175,106,.3); border-radius: 10px; }

/* Splash */
#splash {
  position: fixed; inset: 0; z-index: 9999; display: grid; place-items: center;
  background: #070709; transition: opacity .5s;
}
#splash.hide { opacity: 0; pointer-events: none; }
.splash-mark {
  width: 90px; height: 90px; display: grid; place-items: center;
  font-family: Georgia, serif; font-size: 45px; color: var(--gold);
  border: 1.5px solid var(--line); border-radius: 28px 10px 28px 10px;
  animation: pulse 1.5s infinite;
}
@keyframes pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.05); box-shadow: 0 0 40px rgba(212,175,106,.4); } }

/* Login */
#loginScreen {
  position: fixed; inset: 0; z-index: 2000; display: flex; align-items: flex-start; justify-content: center;
  background: #070709; padding: 20px; overflow-y: auto;
}
.login-card {
  width: 100%; max-width: 360px; margin-top: 40px; margin-bottom: 40px;
  background: linear-gradient(155deg, rgba(255,255,255,.06), rgba(255,255,255,.01));
  border: 1px solid var(--line); border-radius: 24px; padding: 30px 24px;
  box-shadow: 0 20px 60px -15px #000; text-align: center;
}
.login-mark {
  width: 70px; height: 70px; margin: 0 auto 20px; display: grid; place-items: center;
  font-family: Georgia, serif; font-size: 38px; color: var(--gold);
  border: 1.5px solid rgba(212,175,106,.5); border-radius: 20px 6px 20px 6px;
  background: rgba(212,175,106,.08);
}
.login-card h1 { font-size: 22px; font-weight: 900; margin-bottom: 6px; }
.login-card p { color: var(--muted); font-size: 12.5px; margin-bottom: 22px; }
.login-card input {
  width: 100%; padding: 14px; background: rgba(255,255,255,.05);
  border: 1px solid rgba(255,255,255,.12); border-radius: 13px;
  font-size: 16px; text-align: center; letter-spacing: 2px; margin-bottom: 15px;
}
.login-card input:focus { border-color: var(--gold); background: rgba(212,175,106,.08); }

/* App Header */
.appbar {
  flex-shrink: 0; height: var(--appbar-h); z-index: 80;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px; background: rgba(7,7,9,.92);
  border-bottom: 1px solid var(--line-soft); backdrop-filter: blur(20px);
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand-mark {
  width: 38px; height: 38px; display: grid; place-items: center;
  font-family: Georgia, serif; font-size: 22px; color: var(--gold);
  border: 1px solid var(--line); border-radius: 12px 4px 12px 4px;
  background: rgba(212,175,106,.08);
}
.brand-text b { font-size: 15px; font-weight: 900; display: block; line-height: 1.2; }
.brand-text span { font-size: 9.5px; color: var(--muted); }
.status-pill {
  display: flex; align-items: center; gap: 6px; font-size: 10.5px;
  color: var(--muted); border: 1px solid var(--line-soft);
  background: rgba(255,255,255,.03); padding: 6px 12px; border-radius: 100px;
}
.status-dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--ok);
  box-shadow: 0 0 8px var(--ok); animation: pulse-dot 2s infinite;
}
@keyframes pulse-dot { 0% { box-shadow: 0 0 0 0 rgba(107,211,154,.5); } 70% { box-shadow: 0 0 0 8px rgba(107,211,154,0); } 100% { box-shadow: 0 0 0 0 rgba(107,211,154,0); } }

/* Main Content */
main { flex: 1; overflow-y: auto; overflow-x: hidden; padding-bottom: 20px; -webkit-overflow-scrolling: touch; }
.view { display: none; animation: fadeIn .3s ease; }
.view.active { display: block; }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

.wrap { width: min(100% - 32px, 800px); margin-inline: auto; padding-top: 20px; }

/* Bottom Tabbar */
.tabbar {
  flex-shrink: 0; height: var(--tabbar-h); z-index: 100;
  display: flex; align-items: center; padding: 0 6px;
  background: rgba(12,12,16,.98); border-top: 1px solid var(--line-soft);
  backdrop-filter: blur(20px);
  padding-bottom: env(safe-area-inset-bottom);
}
.tab {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px;
  color: var(--muted); font-size: 9px; font-weight: 800; position: relative;
  padding: 4px 0; transition: .2s;
}
.tab svg { width: 22px; height: 22px; stroke: currentColor; fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; transition: transform .2s; }
.tab.active { color: var(--gold); }
.tab.active svg { transform: translateY(-2px); }

/* UI Elements */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  min-height: 48px; padding: 10px 20px; border-radius: 14px;
  font-size: 13px; font-weight: 800; transition: .2s;
}
.btn:active { transform: scale(.96); }
.btn-gold { color: #1b1408; background: linear-gradient(135deg, var(--gold-2), var(--gold) 55%, #c99f55); box-shadow: 0 8px 25px -10px rgba(212,175,106,.8); }
.btn-ghost { color: var(--text); border: 1px solid var(--line-soft); background: rgba(255,255,255,.03); }
.btn-danger { color: #ffd4d4; border: 1px solid rgba(224,107,107,.3); background: rgba(224,107,107,.1); }
.btn-sm { min-height: 36px; padding: 6px 14px; font-size: 11.5px; border-radius: 10px; }
.btn-block { width: 100%; }

.card { background: linear-gradient(155deg, rgba(255,255,255,.05), rgba(255,255,255,.01)); border: 1px solid var(--line-soft); border-radius: var(--r); padding: 20px; margin-bottom: 15px; }
.field { margin-bottom: 16px; }
.field label { display: block; font-size: 12px; font-weight: 800; margin-bottom: 8px; color: #d9d5cc; }
input[type=text], input[type=password], select, textarea {
  width: 100%; padding: 14px; background: rgba(255,255,255,.05);
  border: 1px solid rgba(255,255,255,.1); border-radius: 12px;
  font-size: 16px; transition: .2s;
}
input:focus, select:focus, textarea:focus { border-color: var(--gold); background: rgba(212,175,106,.08); }
textarea { resize: vertical; min-height: 100px; font-family: 'JetBrains Mono', monospace; font-size: 12px; }

/* Stats */
.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
.stat-box { background: var(--card); border: 1px solid var(--line-soft); padding: 16px; border-radius: var(--r-sm); text-align: center; }
.stat-box b { display: block; font-size: 26px; font-weight: 900; color: var(--gold); line-height: 1.2; }
.stat-box span { font-size: 11.5px; color: var(--muted); font-weight: 700; }

/* Table */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th { text-align: start; padding: 10px 12px; font-size: 11px; font-weight: 800; color: var(--muted); border-bottom: 1px solid var(--line-soft); }
td { padding: 12px; border-bottom: 1px solid var(--line-soft); vertical-align: middle; }
tr:hover td { background: rgba(212,175,106,.03); }
.mono { font-family: 'JetBrains Mono', monospace; font-size: 11.5px; color: var(--gold-2); }

/* Feed */
.feed { max-height: 400px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
.feed-item { padding: 12px; border-radius: 12px; background: rgba(255,255,255,.025); border: 1px solid var(--line-soft); font-size: 12px; display: flex; gap: 10px; align-items: center; }
.feed-item .tag { font-size: 10px; font-weight: 800; padding: 2px 8px; border-radius: 6px; background: rgba(212,175,106,.15); color: var(--gold-2); }
.feed-item.insert .tag { background: rgba(107,211,154,.15); color: var(--ok); }
.feed-item.delete .tag { background: rgba(224,107,107,.15); color: var(--danger); }
.feed-item .time { color: var(--muted-2); font-size: 10.5px; font-family: 'JetBrains Mono', monospace; margin-inline-start: auto; }

/* Chart */
.chart { display: flex; align-items: flex-end; gap: 4px; height: 100px; padding-top: 10px; }
.chart-bar { flex: 1; background: linear-gradient(180deg, var(--gold), var(--gold-3)); border-radius: 4px 4px 0 0; min-height: 3px; transition: transform .2s; }
.chart-bar:hover { transform: scaleY(1.08); background: var(--gold-2); }

/* Playground */
.play-result { padding: 14px; background: #050508; border: 1px solid var(--line-soft); border-radius: 10px; font-family: 'JetBrains Mono', monospace; font-size: 11.5px; color: #d1ccc0; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow: auto; }

/* Toast & Modal */
#toasts { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%); z-index: 500; display: flex; flex-direction: column; gap: 8px; width: min(400px, calc(100% - 32px)); pointer-events: none; }
.toast { padding: 12px 18px; border-radius: 12px; font-size: 12.5px; font-weight: 700; background: rgba(22,22,28,.96); border: 1px solid rgba(255,255,255,.12); box-shadow: 0 20px 40px -16px #000; backdrop-filter: blur(14px); opacity: 0; transform: translateY(12px); transition: opacity .3s, transform .3s; }
.toast.in { opacity: 1; transform: none; }
.toast.ok { border-color: rgba(107,211,154,.4); color: #c9f4dc; }
.toast.err { border-color: rgba(224,107,107,.45); color: #ffd4d4; }

.modal { position: fixed; inset: 0; z-index: 400; display: none; align-items: center; justify-content: center; padding: 20px; background: rgba(4,4,6,.85); backdrop-filter: blur(10px); }
.modal.open { display: flex; animation: fadeIn .25s both; }
.modal-box { position: relative; width: min(420px, 100%); padding: 24px; border-radius: 22px; background: linear-gradient(165deg, #141420, #0a0a10); border: 1px solid var(--line); box-shadow: 0 40px 100px -30px #000; }
.modal-x { position: absolute; top: 12px; inset-inline-end: 12px; width: 32px; height: 32px; border-radius: 10px; display: grid; place-items: center; background: rgba(10,10,14,.8); color: var(--text); border: 1px solid rgba(255,255,255,.14); font-size: 14px; }

@media (min-width: 600px) {
  .stat-grid { grid-template-columns: repeat(4, 1fr); }
}
</style>
</head>
<body>

<!-- Splash -->
<div id="splash">
  <div class="splash-mark">D</div>
</div>

<!-- Login -->
<div id="loginScreen">
  <div class="login-card">
    <div class="login-mark">D</div>
    <h1>DSS Server</h1>
    <p>لوحة تحكم قاعدة البيانات — أدخل كلمة المرور</p>
    <input type="password" id="loginPass" placeholder="••••••••" autocomplete="current-password">
    <button class="btn btn-gold btn-block" id="loginBtn">دخول ←</button>
  </div>
</div>

<!-- App Wrapper -->
<div id="app" style="display: none; height: 100%; flex-direction: column;">
  <!-- Header -->
  <header class="appbar">
    <div class="brand">
      <span class="brand-mark">D</span>
      <span class="brand-text">
        <b>DSS <span class="gold">Control</span></b>
        <span>Database-as-a-Service</span>
      </span>
    </div>
    <div class="status-pill">
      <span class="status-dot"></span>
      <span>متصل</span>
    </div>
  </header>

  <!-- Main Content -->
  <main>
    <!-- Overview -->
    <section class="view active" id="view-overview">
      <div class="wrap">
        <div class="stat-grid">
          <div class="stat-box"><b id="sKeys">0</b><span>مفاتيح نشطة</span></div>
          <div class="stat-box"><b id="sDocs">0</b><span>مستندات</span></div>
          <div class="stat-box"><b id="sReqs">0</b><span>طلبات API</span></div>
          <div class="stat-box"><b id="sUptime">0</b><span>مدة التشغيل</span></div>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">نشاط آخر 24 ساعة</div></div>
          <div class="chart" id="chart"></div>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">آخر الأحداث</div><button class="btn btn-ghost btn-sm" onclick="loadActivity()">تحديث</button></div>
          <div class="feed" id="recentActivity"></div>
        </div>
      </div>
    </section>

    <!-- Keys -->
    <section class="view" id="view-keys">
      <div class="wrap">
        <div class="card">
          <div class="card-head">
            <div class="card-title">مفاتيح API</div>
            <button class="btn btn-gold btn-sm" onclick="openNewKeyModal()">+ مفتاح جديد</button>
          </div>
          <div class="table-wrap">
            <table><thead><tr><th>الاسم</th><th>المفتاح</th><th>الطلبات</th><th>الحالة</th><th>الإنشاء</th><th></th></tr></thead>
            <tbody id="keysTable"></tbody></table>
          </div>
        </div>
      </div>
    </section>

    <!-- Data Explorer -->
    <section class="view" id="view-data">
      <div class="wrap">
        <div class="card">
          <div class="card-head"><div class="card-title">مستكشف البيانات</div></div>
          <div class="field"><label>المفتاح</label><select id="dataKey"></select></div>
          <div class="field"><label>المجموعة</label><select id="dataCollection"><option value="">كل المجموعات</option></select></div>
          <button class="btn btn-gold btn-block" onclick="loadData()">تحميل المستندات</button>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">المستندات</div><span class="badge gold" id="dataCount" style="background:rgba(212,175,106,.15);color:var(--gold-2);padding:2px 8px;border-radius:10px;font-size:11px;">0</span></div>
          <div id="dataList" style="display:flex;flex-direction:column;gap:10px"></div>
        </div>
      </div>
    </section>

    <!-- Live -->
    <section class="view" id="view-live">
      <div class="wrap">
        <div class="card">
          <div class="card-head">
            <div class="card-title"><span class="status-dot" style="display:inline-block;margin-inline-end:8px"></span>البث اللحظي</div>
            <span class="badge ok" id="liveStatus" style="background:rgba(107,211,154,.15);color:var(--ok);padding:2px 8px;border-radius:10px;font-size:11px;">متصل</span>
          </div>
          <div class="feed" id="liveFeed"></div>
        </div>
      </div>
    </section>

    <!-- Playground -->
    <section class="view" id="view-playground">
      <div class="wrap">
        <div class="card">
          <div class="card-head"><div class="card-title">اختبار API</div></div>
          <div style="display:flex; gap:10px; margin-bottom:12px;">
            <select id="pgMethod" style="flex: 0 0 100px;">
              <option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option>
            </select>
            <input type="text" id="pgPath" placeholder="/api/db/posts" style="flex: 1;">
          </div>
          <div class="field"><label>المفتاح</label><select id="pgKey"></select></div>
          <div class="field"><label>الجسم (JSON) — للطلبات POST/PUT/PATCH</label><textarea id="pgBody" placeholder='{"title":"مرحبًا"}'></textarea></div>
          <button class="btn btn-gold btn-block" onclick="runPlayground()">إرسال الطلب ←</button>
          <div class="field" style="margin-top: 15px;"><label>الاستجابة</label><div class="play-result" id="pgResult">لم يتم التنفيذ بعد...</div></div>
        </div>
      </div>
    </section>

    <!-- Settings -->
    <section class="view" id="view-settings">
      <div class="wrap">
        <div class="card">
          <div class="card-head"><div class="card-title">تغيير كلمة المرور</div></div>
          <div class="field"><label>كلمة المرور الحالية</label><input type="password" id="pwOld"></div>
          <div class="field"><label>كلمة المرور الجديدة</label><input type="password" id="pwNew" placeholder="6 أحرف على الأقل"></div>
          <div class="field"><label>تأكيد كلمة المرور</label><input type="password" id="pwNew2"></div>
          <button class="btn btn-gold btn-block" onclick="changePassword()">تحديث كلمة المرور</button>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">النسخ الاحتياطي</div></div>
          <p class="muted" style="color:var(--muted); font-size:12.5px; margin-bottom:14px;">نزّل كل مفاتيحك ومستنداتك كملف JSON، أو استعِد نسخة سابقة.</p>
          <div class="row" style="display: flex; gap:10px;">
            <a class="btn btn-ghost btn-sm" style="flex:1;" href="/api/admin/export" download>⬇ تصدير الكل</a>
            <label class="btn btn-ghost btn-sm" style="flex:1; cursor:pointer; text-align:center; margin:0;">
              ⬆ استيراد ملف<input type="file" id="importFile" accept=".json" style="display:none">
            </label>
          </div>
        </div>
        <div class="card" style="border-color: rgba(224,107,107,.3);">
          <div class="card-head"><div class="card-title" style="color:var(--danger);">تسجيل الخروج</div></div>
          <button class="btn btn-danger btn-block" id="logoutBtn">خروج من الحساب</button>
        </div>
      </div>
    </section>
  </main>

  <!-- Tabbar -->
  <nav class="tabbar">
    <button class="tab active" data-tab="overview">
      <svg viewBox="0 0 24 24"><path d="M3 10.6 12 3l9 7.6V21h-6v-6H9v6H3z"/></svg>
      <span>نظرة عامة</span>
    </button>
    <button class="tab" data-tab="keys">
      <svg viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="M10.8 12.2 21 2"/><path d="M15 7l3 3"/></svg>
      <span>المفاتيح</span>
    </button>
    <button class="tab" data-tab="data">
      <svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/></svg>
      <span>البيانات</span>
    </button>
    <button class="tab" data-tab="live">
      <svg viewBox="0 0 24 24"><path d="M13 2 3 14h8l-1 8 10-12h-8z"/></svg>
      <span>البث</span>
    </button>
    <button class="tab" data-tab="playground">
      <svg viewBox="0 0 24 24"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg>
      <span>اختبار</span>
    </button>
    <button class="tab" data-tab="settings">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      <span>الإعدادات</span>
    </button>
  </nav>
</div>

<!-- Modal New Key -->
<div class="modal" id="newKeyModal">
  <div class="modal-box">
    <button class="modal-x" onclick="closeModal('newKeyModal')">✕</button>
    <div class="card-title" style="margin-bottom:14px;">مفتاح API جديد</div>
    <div class="field"><label>اسم المفتاح</label><input type="text" id="newKeyName" placeholder="مثال: موقعي الشخصي"></div>
    <button class="btn btn-gold btn-block" onclick="createKey()">إنشاء المفتاح ←</button>
    <div id="newKeyResult" style="margin-top:14px; display:none;">
      <label>المفتاح الجديد — انسخه الآن</label>
      <div class="play-result" id="newKeyValue" style="cursor:pointer;" onclick="copyKey()"></div>
    </div>
  </div>
</div>

<div id="toasts"></div>

<script>
"use strict";
const $ = s => document.querySelector(s), $$ = s => Array.from(document.querySelectorAll(s));
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtDate = ts => new Date(ts * 1000).toLocaleString('ar', {dateStyle: 'short', timeStyle: 'short'});
const fmtDur = s => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d+'ي ' : '') + (h ? h+'س ' : '') + m+'د'; };

function toast(msg, type = 'ok') {
  const box = $('#toasts'), el = document.createElement('div');
  el.className = 'toast ' + type; el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('in'));
  setTimeout(() => { el.classList.remove('in'); setTimeout(() => el.remove(), 350); }, 3000);
}

function openModal(id) { $('#' + id).classList.add('open'); }
function closeModal(id) { $('#' + id).classList.remove('open'); }

// Splash
setTimeout(() => {
  const sp = $('#splash');
  if (sp) { sp.classList.add('hide'); setTimeout(() => sp.remove(), 600); }
}, 1200);

// Login
$('#loginBtn').onclick = async () => {
  const pass = $('#loginPass').value;
  if (!pass) return;
  const r = await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pass }) });
  const d = await r.json();
  if (d.ok) { startApp(); } else { toast('كلمة المرور غير صحيحة', 'err'); $('#loginPass').value = ''; }
};
$('#loginPass').addEventListener('keydown', e => { if (e.key === 'Enter') $('#loginBtn').click(); });
$('#logoutBtn').onclick = async () => { await fetch('/logout', { method: 'POST' }); location.reload(); };

function startApp() {
  $('#loginScreen').style.display = 'none';
  $('#app').style.display = 'flex';
  loadStats(); loadKeys(); loadActivity();
  openSSE();
}

// Tab Navigation
$$('.tab').forEach(tab => {
  tab.onclick = () => {
    $$('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const view = tab.dataset.tab;
    $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
    if (view === 'overview') { loadStats(); loadActivity(); }
    if (view === 'keys') loadKeys();
    if (view === 'data') loadDataKeys();
    if (view === 'playground') loadPlaygroundKeys();
  };
});

// Stats
async function loadStats() {
  const r = await fetch('/api/admin/stats');
  if (!r.ok) return;
  const d = await r.json();
  $('#sKeys').textContent = d.keys;
  $('#sDocs').textContent = d.documents;
  $('#sReqs').textContent = d.requests;
  $('#sUptime').textContent = fmtDur(d.uptime);
  drawChart(d.by_hour);
}

function drawChart(data) {
  const chart = $('#chart'), max = Math.max(1, ...data.map(x => x.count));
  chart.innerHTML = data.map(x => `<div class="chart-bar" style="height:${Math.max(4, x.count / max * 100)}%" title="${x.count}"></div>`).join('');
}

// Activity
async function loadActivity() {
  const r = await fetch('/api/admin/activity?limit=20');
  const d = await r.json();
  const box = $('#recentActivity');
  if (!d.activity.length) { box.innerHTML = '<div style="padding:20px;color:var(--muted);text-align:center">لا يوجد نشاط بعد</div>'; return; }
  box.innerHTML = d.activity.map(a => {
    const cls = a.action.includes('delete') ? 'delete' : a.action.includes('insert') ? 'insert' : '';
    return `<div class="feed-item ${cls}"><span class="tag">${esc(a.action)}</span><span>${esc(a.collection || '—')}</span><span style="color:var(--muted);font-size:11px">${esc((a.detail || '').slice(0,40))}</span><span class="time">${fmtDate(a.ts)}</span></div>`;
  }).join('');
}

// Keys
async function loadKeys() {
  const r = await fetch('/api/admin/keys');
  const d = await r.json();
  const tb = $('#keysTable');
  if (!d.keys.length) { tb.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:20px">لا توجد مفاتيح</td></tr>'; return; }
  tb.innerHTML = d.keys.map(k => `
    <tr>
      <td><b>${esc(k.name)}</b></td>
      <td class="mono">${esc(k.api_key.slice(0,8))}…${esc(k.api_key.slice(-4))}</td>
      <td>${k.requests || 0}</td>
      <td><span class="badge ${k.active ? 'ok' : 'off'}" style="padding:2px 8px;border-radius:10px;font-size:10px;background:${k.active?'rgba(107,211,154,.15);color:var(--ok)':'rgba(224,107,107,.15);color:var(--danger)'}">${k.active ? 'نشط' : 'موقوف'}</span></td>
      <td style="color:var(--muted);font-size:11px">${fmtDate(k.created_at)}</td>
      <td style="text-align:end">
        <button class="btn btn-ghost btn-sm" onclick="copyText('${esc(k.api_key)}')">📋</button>
        <button class="btn btn-ghost btn-sm" onclick="toggleKey('${esc(k.api_key)}')">${k.active ? '⏸' : '▶'}</button>
        <button class="btn btn-danger btn-sm" onclick="deleteKey('${esc(k.api_key)}','${esc(k.name)}')">🗑</button>
      </td>
    </tr>
  `).join('');
}

function copyText(t) { navigator.clipboard.writeText(t).then(() => toast('تم النسخ ✓', 'ok')); }
async function toggleKey(k) { const r = await fetch('/api/admin/keys/' + k + '/toggle', { method: 'POST' }); if (r.ok) { toast('تم التحديث', 'ok'); loadKeys(); } }
async function deleteKey(k, name) { if (!confirm('حذف المفتاح "' + name + '" وكل مستنداته؟')) return; const r = await fetch('/api/admin/keys/' + k, { method: 'DELETE' }); if (r.ok) { toast('تم الحذف', 'ok'); loadKeys(); loadStats(); } }

function openNewKeyModal() { openModal('newKeyModal'); $('#newKeyResult').style.display = 'none'; $('#newKeyName').value = ''; }
async function createKey() {
  const name = $('#newKeyName').value.trim() || 'مفتاح جديد';
  const r = await fetch('/api/admin/keys', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
  const d = await r.json();
  if (d.ok) { $('#newKeyValue').textContent = d.key; $('#newKeyResult').style.display = 'block'; toast('تم إنشاء المفتاح ✓', 'ok'); loadKeys(); }
}
function copyKey() { copyText($('#newKeyValue').textContent); }

// Data Explorer
async function loadDataKeys() {
  const r = await fetch('/api/admin/keys');
  const d = await r.json();
  const sel = $('#dataKey');
  sel.innerHTML = d.keys.filter(k => k.active).map(k => `<option value="${esc(k.api_key)}">${esc(k.name)}</option>`).join('');
  if (sel.value) loadCollections();
  sel.onchange = loadCollections;
}

async function loadCollections() {
  const key = $('#dataKey').value;
  if (!key) return;
  const r = await fetch('/api/admin/collections?key=' + encodeURIComponent(key));
  const d = await r.json();
  const sel = $('#dataCollection');
  sel.innerHTML = '<option value="">كل المجموعات</option>' + d.collections.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
}

async function loadData() {
  const key = $('#dataKey').value, col = $('#dataCollection').value;
  if (!key) { toast('اختر مفتاحًا أولًا', 'err'); return; }
  const r = await fetch('/api/admin/documents?key=' + encodeURIComponent(key) + '&collection=' + encodeURIComponent(col));
  const d = await r.json();
  $('#dataCount').textContent = d.documents.length;
  const box = $('#dataList');
  if (!d.documents.length) { box.innerHTML = '<div style="text-align:center;color:var(--muted);padding:30px;font-size:13px">لا توجد مستندات</div>'; return; }
  box.innerHTML = d.documents.map(doc => `
    <div class="card" style="margin:0;padding:14px;background:#0a0a0f">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div class="mono" style="color:var(--gold-2)">${esc(doc.id)}</div>
        <div style="display:flex;gap:6px;align-items:center">
          <span class="badge gold" style="background:rgba(212,175,106,.15);color:var(--gold-2);padding:2px 8px;border-radius:10px;font-size:10px;">${esc(doc.collection)}</span>
          <button class="btn btn-danger btn-sm" onclick="deleteDoc('${esc(doc.id)}')">🗑</button>
        </div>
      </div>
      <pre style="font-family:'JetBrains Mono',monospace;font-size:11.5px;color:#d1ccc0;white-space:pre-wrap;word-break:break-all;margin:0">${esc(JSON.stringify(doc.data, null, 2))}</pre>
    </div>
  `).join('');
}

async function deleteDoc(id) {
  if (!confirm('حذف المستند؟')) return;
  const r = await fetch('/api/admin/documents/' + id, { method: 'DELETE' });
  if (r.ok) { toast('تم الحذف', 'ok'); loadData(); loadStats(); }
}

// Playground
async function loadPlaygroundKeys() {
  const r = await fetch('/api/admin/keys');
  const d = await r.json();
  $('#pgKey').innerHTML = d.keys.filter(k => k.active).map(k => `<option value="${esc(k.api_key)}">${esc(k.name)}</option>`).join('');
}

async function runPlayground() {
  const method = $('#pgMethod').value, path = $('#pgPath').value, key = $('#pgKey').value, body = $('#pgBody').value.trim();
  if (!path) { toast('أدخل مسارًا', 'err'); return; }
  const opts = { method, headers: { 'X-API-Key': key } };
  if (['POST', 'PUT', 'PATCH'].includes(method) && body) { opts.headers['Content-Type'] = 'application/json'; opts.body = body; }
  try {
    const r = await fetch(path, opts);
    const text = await r.text();
    let out; try { out = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { out = text; }
    $('#pgResult').textContent = 'HTTP ' + r.status + '\n\n' + out;
  } catch (e) { $('#pgResult').textContent = 'خطأ: ' + e.message; }
}

// SSE
let es = null;
function openSSE() {
  if (es) es.close();
  es = new EventSource('/api/admin/stream');
  es.addEventListener('hello', () => { $('#liveStatus').textContent = 'متصل'; $('#liveStatus').style.color = 'var(--ok)'; });
  es.addEventListener('activity', e => {
    try {
      const data = JSON.parse(e.data), feed = $('#liveFeed'), item = document.createElement('div');
      const cls = data.action.includes('delete') ? 'delete' : data.action.includes('insert') ? 'insert' : '';
      item.className = 'feed-item ' + cls;
      item.innerHTML = `<span class="tag">${esc(data.action)}</span><span>${esc(data.collection || '—')}</span><span style="color:var(--muted);font-size:11px">${esc((data.detail || '').slice(0,50))}</span><span class="time">${fmtDate(data.ts)}</span>`;
      feed.insertBefore(item, feed.firstChild);
      while (feed.children.length > 100) feed.removeChild(feed.lastChild);
    } catch (e) {}
  });
  es.onerror = () => { $('#liveStatus').textContent = 'منقطع'; $('#liveStatus').style.color = 'var(--danger)'; setTimeout(openSSE, 3000); };
}

// Settings
async function changePassword() {
  const old = $('#pwOld').value, nw = $('#pwNew').value, nw2 = $('#pwNew2').value;
  if (nw !== nw2) { toast('تأكيد كلمة المرور غير مطابق', 'err'); return; }
  if (nw.length < 6) { toast('6 أحرف على الأقل', 'err'); return; }
  const r = await fetch('/api/admin/password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ old, new: nw }) });
  if (r.ok) { toast('تم التحديث ✓', 'ok'); $('#pwOld').value = $('#pwNew').value = $('#pwNew2').value = ''; }
  else toast('كلمة المرور الحالية غير صحيحة', 'err');
}

$('#importFile').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const text = await f.text();
  try {
    const data = JSON.parse(text);
    const r = await fetch('/api/admin/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    const d = await r.json();
    if (d.ok) { toast(`تم الاستيراد: ${d.keys} مفتاح، ${d.documents} مستند`, 'ok'); loadKeys(); loadStats(); }
    else toast('فشل الاستيراد', 'err');
  } catch (e) { toast('الملف غير صالح', 'err'); }
};

// Check session
(async () => {
  const r = await fetch('/api/admin/check');
  const d = await r.json();
  if (d.admin) startApp();
})();
</script>
</body>
</html>
"""

# ==========================================================
# تشغيل
# ==========================================================
if __name__ == "__main__":
    print(f"\n🚀 DSS Server على http://{HOST}:{PORT}", flush=True)
    print(f"   كلمة المرور: {ADMIN_PASS_ENV}", flush=True)
    if SELF_URL:
        print(f"   Keep-Alive: {SELF_URL}/api/ping كل {PING_INTERVAL}ث", flush=True)
    else:
        print("   ⚠ Keep-Alive معطّل — أضف SELF_URL أو RENDER_EXTERNAL_URL", flush=True)
    print(flush=True)
    app.run(host=HOST, port=PORT, threaded=True)
