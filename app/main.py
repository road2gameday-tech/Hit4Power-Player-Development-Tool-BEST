# app/main.py
import os
import sqlite3
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Form, Depends
from fastapi import Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

# --- imports (top of file, with your other FastAPI imports) ---
from fastapi import Form, HTTPException
from starlette.responses import RedirectResponse
import random, string

# --- helper: make a unique 6-digit login code ---
def _make_login_code(conn, length: int = 6) -> str:
    for _ in range(50):
        code = "".join(random.choices(string.digits, k=length))
        exists = conn.execute(
            "SELECT 1 FROM players WHERE login_code = ?", (code,)
        ).fetchone()
        if not exists:
            return code
    raise RuntimeError("Could not generate unique login code")

# --- route: create a player (form posts here) ---
@app.post("/players/create")
def create_player(request: Request, name: str = Form(...)):
    # must be an instructor
    try:
        instructor_id = require_instructor(request)
    except RedirectResponse as r:
        return r

    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    conn = get_db()
    try:
        login_code = _make_login_code(conn)
        conn.execute(
            """
            INSERT INTO players (name, login_code, created_at, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (name, login_code),
        )
        conn.commit()
    finally:
        conn.close()

    # back to the instructor dashboard
    return RedirectResponse(url="/instructor", status_code=303)

# -----------------------------------------------------------------------------
# App & Templating
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "app.db"))
SECRET_KEY = os.environ.get("SESSION_SECRET", "dev-secret-change-me")

app = FastAPI(title="Hit4Power")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

def datetimeformat(value: Any) -> str:
    """Jinja filter for nice date/time printouts."""
    if not value:
        return ""
    try:
        if isinstance(value, (datetime, date)):
            dt = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
        else:
            # Accept ISO strings
            dt = datetime.fromisoformat(str(value))
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return str(value)

templates.env.filters["datetimeformat"] = datetimeformat

# -----------------------------------------------------------------------------
# DB utils & schema
# -----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def col_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == column for r in cur.fetchall())

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def ensure_schema() -> None:
    conn = get_db()
    cur = conn.cursor()

    # players
    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            login_code TEXT UNIQUE,
            image_path TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

    # instructors (one default instructor)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS instructors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # seed default instructor if none
    cur.execute("SELECT COUNT(*) AS c FROM instructors")
    if cur.fetchone()["c"] == 0:
        cur.execute("INSERT INTO instructors (name, email) VALUES (?, ?)", ("Coach", "coach@example.com"))

    # favorites
    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            instructor_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            PRIMARY KEY (instructor_id, player_id),
            FOREIGN KEY (instructor_id) REFERENCES instructors(id),
            FOREIGN KEY (player_id) REFERENCES players(id)
        );
    """)

    # notes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            instructor_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            shared INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # add kind column if missing (some templates used it earlier)
    if not col_exists(conn, "notes", "kind"):
        cur.execute("ALTER TABLE notes ADD COLUMN kind TEXT")

    # drills (library)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # seed a couple of drills if empty
    cur.execute("SELECT COUNT(*) AS c FROM drills")
    if cur.fetchone()["c"] == 0:
        cur.executemany(
            "INSERT INTO drills (title, description) VALUES (?, ?)",
            [
                ("Med Ball Rotational Throws", "Explosive hip rotation with med ball."),
                ("One-Handed Tee Work", "Focus on barrel control and path."),
                ("Step-Back BP", "Load timing and momentum into launch.")
            ],
        )

    # drill assignments
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drill_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            instructor_id INTEGER NOT NULL,
            drill_id INTEGER NOT NULL,
            note TEXT,
            status TEXT DEFAULT 'assigned',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # add due_date if missing
    if not col_exists(conn, "drill_assignments", "due_date"):
        cur.execute("ALTER TABLE drill_assignments ADD COLUMN due_date TEXT")

    # metrics (generic, tall table: metric -> value)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            metric TEXT,
            value REAL,
            unit TEXT,
            recorded_at TEXT DEFAULT (datetime('now')),
            source TEXT,
            entered_by_instructor_id INTEGER,
            note TEXT
        );
    """)
    # Backfill columns if this DB existed before
    for col, ddl in [
        ("metric", "ALTER TABLE metrics ADD COLUMN metric TEXT"),
        ("value", "ALTER TABLE metrics ADD COLUMN value REAL"),
        ("unit", "ALTER TABLE metrics ADD COLUMN unit TEXT"),
        ("source", "ALTER TABLE metrics ADD COLUMN source TEXT"),
        ("entered_by_instructor_id", "ALTER TABLE metrics ADD COLUMN entered_by_instructor_id INTEGER"),
        ("note", "ALTER TABLE metrics ADD COLUMN note TEXT"),
    ]:
        if not col_exists(conn, "metrics", col):
            cur.execute(ddl)

    # ensure recorded_at has a value
    cur.execute("UPDATE metrics SET recorded_at = COALESCE(recorded_at, datetime('now')) WHERE recorded_at IS NULL")

    conn.commit()
    conn.close()

ensure_schema()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def require_instructor(request: Request) -> int:
    iid = request.session.get("instructor_id")
    if not iid:
        # bounce to home
        raise RedirectResponse(url="/", status_code=303)
    return int(iid)

def require_player(request: Request) -> int:
    pid = request.session.get("player_id")
    if not pid:
        raise RedirectResponse(url="/", status_code=303)
    return int(pid)

def ensure_login_code(name: str) -> str:
    # simple stable code from name + time
    base = "".join(ch for ch in name.lower() if ch.isalnum())
    return (base[:3] + datetime.utcnow().strftime("%H%M%S"))[-6:]

def pivot_metrics(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """
    Turn tall metrics rows (metric, value, recorded_at) into
    [{date, exit_velocity, launch_angle, spin_rate}, ...] by day.
    """
    by_day: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        rec = r["recorded_at"] or now_iso()
        try:
            dkey = datetime.fromisoformat(rec).date().isoformat()
        except Exception:
            dkey = str(rec)[:10]
        if dkey not in by_day:
            by_day[dkey] = {"date": dkey, "exit_velocity": None, "launch_angle": None, "spin_rate": None}
        m = (r["metric"] or "").strip().lower()
        val = r["value"]
        if m in ("ev", "exit_velocity", "exit-velocity"):
            by_day[dkey]["exit_velocity"] = val
        elif m in ("la", "launch_angle", "launch-angle"):
            by_day[dkey]["launch_angle"] = val
        elif m in ("sr", "spin_rate", "spin-rate"):
            by_day[dkey]["spin_rate"] = val
    out = list(by_day.values())
    # sort by date
    out.sort(key=lambda x: x["date"])
    return out

# -----------------------------------------------------------------------------
# Health / Readiness / Probes (prevents 502 & 405 on HEAD)
# -----------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

@app.get("/ready", include_in_schema=False)
def ready():
    # optionally test DB connectivity
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return {"ready": True}
    except Exception as e:
        return JSONResponse({"ready": False, "error": str(e)}, status_code=500)

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

# -----------------------------------------------------------------------------
# Home / Index (resilient)
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    try:
        return templates.TemplateResponse("index.html", {"request": request})
    except Exception:
        # Minimal OK page so the LB stops 502’ing while templates settle
        return HTMLResponse("<h1>Hit4Power</h1><p>App is running.</p>", status_code=200)

# -----------------------------------------------------------------------------
# Auth (simple session stubs)
# -----------------------------------------------------------------------------
@app.post("/login/instructor")
def login_instructor(request: Request):
    # Trust the form; set instructor 1
    request.session["instructor_id"] = 1
    return RedirectResponse("/instructor", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

@app.post("/login/player")
def login_player(request: Request, code: str = Form(...)):
    conn = get_db()
    cur = conn.execute("SELECT id FROM players WHERE login_code = ?", (code.strip(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        # send back to home if invalid
        return RedirectResponse("/", status_code=303)
    request.session["player_id"] = int(row["id"])
    return RedirectResponse("/dashboard", status_code=303)

# -----------------------------------------------------------------------------
# Instructor dashboard & player management
# -----------------------------------------------------------------------------
from collections import OrderedDict
import string

@app.get("/instructor", response_class=HTMLResponse)
def instructor_home(request: Request):
    try:
        iid = require_instructor(request)
    except RedirectResponse as r:
        return r

    conn = get_db()
    cur = conn.execute("SELECT id, name, login_code, image_path FROM players ORDER BY name COLLATE NOCASE")
    players = cur.fetchall()

    # Build (player_row, sessions_count, is_favorited) triples
    arr = []
    for p in players:
        sessions_count = conn.execute(
            "SELECT COUNT(DISTINCT date(recorded_at)) AS c FROM metrics WHERE player_id = ?",
            (p["id"],),
        ).fetchone()["c"]
        is_fav = conn.execute(
            "SELECT 1 FROM favorites WHERE instructor_id = ? AND player_id = ?",
            (iid, p["id"]),
        ).fetchone() is not None
        arr.append((p, int(sessions_count or 0), is_fav))

    # ---- Group into ⭐ Favorites, A–Z, and # (non-alpha) ----
    favorites: list = []
    letter_buckets: dict = {}
    for triple in arr:
        p = triple[0]
        name = (p["name"] or "").strip()
        if triple[2]:
            favorites.append(triple)

        # pick first alphabetic character; else '#'
        first_alpha = "#"
        for ch in name:
            if ch.isalpha():
                first_alpha = ch.upper()
                break
        if first_alpha not in string.ascii_uppercase:
            first_alpha = "#"

        letter_buckets.setdefault(first_alpha, []).append(triple)

    # sort each bucket by player name
    for k in letter_buckets:
        letter_buckets[k].sort(key=lambda t: (t[0]["name"] or "").lower())
    favorites.sort(key=lambda t: (t[0]["name"] or "").lower())

    # Ordered dict with Favorites first, then A–Z, then '#'
    grouped = OrderedDict()
    if favorites:
        grouped["★ Favorites"] = favorites
    for L in string.ascii_uppercase:
        if L in letter_buckets:
            grouped[L] = letter_buckets[L]
    if "#" in letter_buckets:
        grouped["#"] = letter_buckets["#"]

    conn.close()

    ctx = {
        "request": request,
        "arr": arr,          # keeps older templates happy
        "grouped": grouped,  # new template expects this
    }
    return templates.TemplateResponse("instructor_dashboard.html", ctx)

# -----------------------------------------------------------------------------
# Instructor: player detail, metrics, notes, drills
# -----------------------------------------------------------------------------
@app.get("/instructor/player/{player_id}", response_class=HTMLResponse)
def instructor_player_detail(request: Request, player_id: int):
    try:
        iid = require_instructor(request)
    except RedirectResponse as r:
        return r

    conn = get_db()
    p = conn.execute("SELECT id, name, login_code, image_path FROM players WHERE id = ?", (player_id,)).fetchone()
    if not p:
        conn.close()
        return PlainTextResponse("Player not found", status_code=404)

    # recent tall metrics -> pivot for display list
    mrows = conn.execute(
        """
        SELECT id, player_id, metric, value, unit, recorded_at, source, entered_by_instructor_id, note
        FROM metrics
        WHERE player_id = ?
        ORDER BY recorded_at DESC
        LIMIT 250
        """,
        (player_id,),
    ).fetchall()
    pivoted = pivot_metrics(list(reversed(mrows)))  # chronological for display
    # show newest first in the list
    pivoted.sort(key=lambda x: x["date"], reverse=True)

    notes = conn.execute(
        """
        SELECT id, player_id, instructor_id, text, shared, kind, created_at, updated_at
        FROM notes
        WHERE player_id = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
    (player_id,)).fetchall()

    drills = conn.execute("SELECT id, title FROM drills ORDER BY title COLLATE NOCASE").fetchall()
    conn.close()

    ctx = {
        "request": request,
        "player": p,
        "metrics": pivoted,   # list[{date, exit_velocity, launch_angle, spin_rate}]
        "notes": notes,
        "drills": drills,
    }
    return templates.TemplateResponse("instructor_player_detail.html", ctx)

@app.post("/metrics/add")
def add_metrics(
    request: Request,
    player_id: int = Form(...),
    date: Optional[str] = Form(None),
    exit_velocity: Optional[float] = Form(None),
    launch_angle: Optional[float] = Form(None),
    spin_rate: Optional[float] = Form(None),
):
    try:
        iid = require_instructor(request)
    except RedirectResponse as r:
        return r

    when = date or now_iso()
    # normalize to ISO timestamp if user supplied YYYY-MM-DD
    try:
        if date and len(date) == 10:
            when = datetime.fromisoformat(date).isoformat()
    except Exception:
        when = now_iso()

    conn = get_db()
    ins = []
    if exit_velocity is not None:
        ins.append(("exit_velocity", exit_velocity, "mph"))
    if launch_angle is not None:
        ins.append(("launch_angle", launch_angle, "deg"))
    if spin_rate is not None:
        ins.append(("spin_rate", spin_rate, "rpm"))

    for m, v, unit in ins:
        conn.execute(
            """
            INSERT INTO metrics (player_id, metric, value, unit, recorded_at, source, entered_by_instructor_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (player_id, m, v, unit, when, "manual", iid),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

@app.post("/notes/add")
def add_note(
    request: Request,
    player_id: int = Form(...),
    text: str = Form(...),
    share_with_player: Optional[str] = Form(None),
    text_player: Optional[str] = Form(None),
):
    try:
        iid = require_instructor(request)
    except RedirectResponse as r:
        return r

    shared = 1 if share_with_player else 0
    conn = get_db()
    conn.execute(
        "INSERT INTO notes (player_id, instructor_id, text, shared, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (player_id, iid, text.strip(), shared, now_iso(), now_iso()),
    )
    conn.commit()
    conn.close()

    # (Optional) texting could be implemented here if desired.
    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

@app.post("/drills/assign")
def assign_drill(
    request: Request,
    player_id: int = Form(...),
    drill_id: int = Form(...),
    note: Optional[str] = Form(None),
):
    try:
        iid = require_instructor(request)
    except RedirectResponse as r:
        return r

    conn = get_db()
    conn.execute(
        """
        INSERT INTO drill_assignments (player_id, instructor_id, drill_id, note, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'assigned', ?, ?)
        """,
        (player_id, iid, drill_id, (note or "").strip(), now_iso(), now_iso()),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

# -----------------------------------------------------------------------------
# Player dashboard
# -----------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    try:
        pid = require_player(request)
    except RedirectResponse as r:
        return r

    conn = get_db()
    p = conn.execute("SELECT id, name, login_code, image_path FROM players WHERE id = ?", (pid,)).fetchone()
    if not p:
        conn.close()
        return RedirectResponse("/", status_code=303)

    mrows = conn.execute(
        """
        SELECT metric, value, recorded_at
        FROM metrics
        WHERE player_id = ?
        ORDER BY recorded_at ASC
        """,
        (pid,),
    ).fetchall()
    pivoted = pivot_metrics(mrows)

    # Build chart arrays (always provide empty lists at minimum)
    labels = [row["date"] for row in pivoted]
    exitv = [row.get("exit_velocity") or 0 for row in pivoted]

    # last shared note
    last_note = conn.execute(
        """
        SELECT id, text, created_at
        FROM notes
        WHERE player_id = ? AND shared = 1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (pid,),
    ).fetchone()

    # recent assignments
    assignments = conn.execute(
        """
        SELECT da.id, d.title, da.status, da.created_at, da.due_date
        FROM drill_assignments da
        JOIN drills d ON d.id = da.drill_id
        WHERE da.player_id = ?
        ORDER BY da.created_at DESC
        LIMIT 20
        """,
        (pid,),
    ).fetchall()

    conn.close()

    ctx = {
        "request": request,
        "player": p,
        "labels": labels,
        "data": exitv,        # for older template variants
        "dates": labels,      # for newer template variants
        "exitv": exitv,       # for newer template variants
        "last_note": last_note,
        "assignments": assignments,
    }
    return templates.TemplateResponse("dashboard.html", ctx)

# -----------------------------------------------------------------------------
# Fallback for unknown HEAD probes (optional: uncomment if you still see HEAD 405s)
# -----------------------------------------------------------------------------
# @app.head("/{path:path}", include_in_schema=False)
# def any_head(path: str):
#    return Response(status_code=200)
