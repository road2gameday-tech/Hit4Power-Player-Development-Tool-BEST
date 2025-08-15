# app/main.py
from __future__ import annotations

import os
import random
import sqlite3
import string
from datetime import datetime, date
from typing import Dict, List, Tuple

from fastapi import FastAPI, Request, Form, HTTPException, Response
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

# -----------------------------------------------------------------------------
# App & config
# -----------------------------------------------------------------------------
app = FastAPI()

# Secret for session cookies (replace with env var in prod)
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

# Static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

def _datetimeformat(value, fmt="%Y-%m-%d"):
    """Jinja filter: formats a datetime/date/ISO string to the given format."""
    if value is None or value == "":
        return ""
    if isinstance(value, (datetime, date)):
        dt = value
    else:
        dt = None
        for try_fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(str(value), try_fmt)
                break
            except Exception:
                pass
        if dt is None:
            return str(value)
    return dt.strftime(fmt)

templates.env.filters["datetimeformat"] = _datetimeformat
def _initials(value: str, max_letters: int = 2) -> str:
    """Jinja filter: 'John Q Public' -> 'JQ' (up to max_letters)."""
    s = str(value or "").strip()
    if not s:
        return ""
    parts = [p for p in s.split() if p]
    if not parts:
        return ""
    letters = "".join(p[0] for p in parts[:max_letters])
    return letters.upper()

templates.env.filters["initials"] = _initials

# -----------------------------------------------------------------------------
# DB helpers / schema
# -----------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def _ensure_table(conn: sqlite3.Connection, sql: str):
    conn.execute(sql)

def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cur.fetchall())

def ensure_schema():
    """Create tables and add any missing columns referenced by templates/routes."""
    conn = get_db()
    try:
        # players
        _ensure_table(conn, """
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                login_code TEXT UNIQUE,
                image_path TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # notes
        _ensure_table(conn, """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                instructor_id INTEGER,
                text TEXT NOT NULL,
                shared INTEGER DEFAULT 0,
                kind TEXT DEFAULT 'coach',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        """)
        if not _has_column(conn, "notes", "kind"):
            conn.execute("ALTER TABLE notes ADD COLUMN kind TEXT DEFAULT 'coach'")

        # drills
        _ensure_table(conn, """
            CREATE TABLE IF NOT EXISTS drills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # drill_assignments
        _ensure_table(conn, """
            CREATE TABLE IF NOT EXISTS drill_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                instructor_id INTEGER,
                drill_id INTEGER NOT NULL,
                note TEXT,
                status TEXT DEFAULT 'assigned',
                due_date TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE,
                FOREIGN KEY(drill_id) REFERENCES drills(id) ON DELETE CASCADE
            )
        """)
        if not _has_column(conn, "drill_assignments", "due_date"):
            conn.execute("ALTER TABLE drill_assignments ADD COLUMN due_date TEXT")

        # metrics
        _ensure_table(conn, """
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                recorded_at TEXT,
                date TEXT,
                metric TEXT,
                value REAL,
                unit TEXT,
                source TEXT,
                entered_by_instructor_id INTEGER,
                note TEXT,
                exit_velocity REAL,
                launch_angle REAL,
                spin_rate REAL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        """)
        for col, ddl in [
            ("metric", "ALTER TABLE metrics ADD COLUMN metric TEXT"),
            ("value", "ALTER TABLE metrics ADD COLUMN value REAL"),
            ("unit", "ALTER TABLE metrics ADD COLUMN unit TEXT"),
            ("source", "ALTER TABLE metrics ADD COLUMN source TEXT"),
            ("entered_by_instructor_id", "ALTER TABLE metrics ADD COLUMN entered_by_instructor_id INTEGER"),
            ("note", "ALTER TABLE metrics ADD COLUMN note TEXT"),
            ("recorded_at", "ALTER TABLE metrics ADD COLUMN recorded_at TEXT"),
            ("date", "ALTER TABLE metrics ADD COLUMN date TEXT"),
            ("exit_velocity", "ALTER TABLE metrics ADD COLUMN exit_velocity REAL"),
            ("launch_angle", "ALTER TABLE metrics ADD COLUMN launch_angle REAL"),
            ("spin_rate", "ALTER TABLE metrics ADD COLUMN spin_rate REAL"),
        ]:
            if not _has_column(conn, "metrics", col):
                conn.execute(ddl)

        # favorites
        _ensure_table(conn, """
            CREATE TABLE IF NOT EXISTS instructor_favorites (
                instructor_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (instructor_id, player_id),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        """)

        # seed drills if empty
        existing = conn.execute("SELECT COUNT(*) AS c FROM drills").fetchone()["c"]
        if existing == 0:
            conn.executemany(
                "INSERT INTO drills (title, description) VALUES (?, ?)",
                [
                    ("Top-hand tee", "Focus on top-hand path and contact"),
                    ("Opposite-field T", "Drive to oppo gap, stay inside"),
                    ("Medicine-ball throws", "Explosive hip rotation"),
                ],
            )

        conn.commit()
    finally:
        conn.close()

@app.on_event("startup")
def _on_startup():
    ensure_schema()

# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
from fastapi import Response, HTTPException

@app.get("/health", include_in_schema=False)
def health():
    # super cheap liveness check
    return {"ok": True}

@app.get("/ready", include_in_schema=False)
def ready():
    # verify we can reach SQLite (or your DB)
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return {"ready": True}
    except Exception as e:
        # Surface a 503 so Render keeps probing until we’re actually ready
        raise HTTPException(status_code=503, detail=f"not ready: {e}")

@app.head("/", include_in_schema=False)
def root_head():
    # Render/Google probe with HEAD; avoid template rendering here
    return Response(status_code=200)

def _make_login_code(conn: sqlite3.Connection, length: int = 6) -> str:
    for _ in range(64):
        code = "".join(random.choices(string.digits, k=length))
        row = conn.execute("SELECT 1 FROM players WHERE login_code = ?", (code,)).fetchone()
        if not row:
            return code
    raise RuntimeError("Could not generate unique login code")

def _require_instructor(request: Request) -> int:
    iid = request.session.get("instructor_id")
    if not iid:
        raise HTTPException(status_code=303, detail="Redirect", headers={"Location": "/"})
    return iid

def _require_player(request: Request) -> int:
    pid = request.session.get("player_id")
    if not pid:
        raise HTTPException(status_code=303, detail="Redirect", headers={"Location": "/"})
    return pid

# -----------------------------------------------------------------------------
# Health / Ready / HEAD (for probes)
# -----------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}

@app.get("/ready", include_in_schema=False)
def ready():
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return {"ready": True}
    except Exception:
        return {"ready": False}

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

# -----------------------------------------------------------------------------
# Index & auth
# -----------------------------------------------------------------------------
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/login/instructor")
def login_instructor(request: Request):
    """Simple demo login: set instructor_id=1 in session."""
    request.session["instructor_id"] = 1
    return RedirectResponse("/instructor", status_code=303)

@app.post("/login/player")
def login_player(request: Request, code: str = Form(...)):
    code = (code or "").strip()
    if not code:
        return RedirectResponse("/", status_code=303)
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM players WHERE login_code = ?", (code,)).fetchone()
        if not row:
            return RedirectResponse("/", status_code=303)
        request.session["player_id"] = row["id"]
    finally:
        conn.close()
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

# -----------------------------------------------------------------------------
# Instructor dashboard & actions
# -----------------------------------------------------------------------------
@app.get("/instructor")
def instructor_home(request: Request):
    try:
        iid = _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    conn = get_db()
    try:
        players = conn.execute(
            "SELECT p.* FROM players p ORDER BY p.created_at DESC, p.id DESC"
        ).fetchall()

        fav_rows = conn.execute(
            "SELECT player_id FROM instructor_favorites WHERE instructor_id = ?", (iid,)
        ).fetchall()
        fav_set = {r["player_id"] for r in fav_rows}

        player_ids = [p["id"] for p in players] or [-1]
        placeholders = ",".join(["?"] * len(player_ids))
        counts = {}
        cur = conn.execute(
            f"SELECT player_id, COUNT(*) AS c FROM metrics WHERE player_id IN ({placeholders}) GROUP BY player_id",
            player_ids,
        )
        for r in cur.fetchall():
            counts[r["player_id"]] = r["c"]

        triples: List[Tuple[sqlite3.Row, int, bool]] = []
        for p in players:
            triples.append((p, counts.get(p["id"], 0), p["id"] in fav_set))

        grouped: Dict[str, List[Tuple[sqlite3.Row, int, bool]]] = {
            "Favorites": [t for t in triples if t[2]],
            "All Players": triples,
        }

        ctx = {"request": request, "grouped": grouped}
        return templates.TemplateResponse("instructor_dashboard.html", ctx)
    finally:
        conn.close()

@app.post("/players/create")
def create_player(request: Request, name: str = Form(...)):
    try:
        _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    conn = get_db()
    try:
        code = _make_login_code(conn)
        conn.execute(
            "INSERT INTO players (name, login_code, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
            (name, code),
        )
        conn.commit()
    finally:
        conn.close()

    return RedirectResponse("/instructor", status_code=303)

@app.post("/favorite/{player_id}")
def toggle_favorite(request: Request, player_id: int):
    try:
        iid = _require_instructor(request)
    except HTTPException:
        return JSONResponse({"favorited": False}, status_code=401)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM instructor_favorites WHERE instructor_id=? AND player_id=?",
            (iid, player_id),
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM instructor_favorites WHERE instructor_id=? AND player_id=?",
                (iid, player_id),
            )
            conn.commit()
            return JSONResponse({"favorited": False})
        else:
            conn.execute(
                "INSERT INTO instructor_favorites (instructor_id, player_id) VALUES (?, ?)",
                (iid, player_id),
            )
            conn.commit()
            return JSONResponse({"favorited": True})
    finally:
        conn.close()

@app.get("/instructor/player/{player_id}")
def instructor_player_detail(request: Request, player_id: int):
    # must be logged in as instructor
    try:
        _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    conn = get_db()
    try:
        # ensure rows are dict-like for Jinja access
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        cur = conn.cursor()

        # --- core data ---
        player = cur.execute(
            "SELECT * FROM players WHERE id = ?",
            (player_id,),
        ).fetchone()
        if not player:
            raise HTTPException(status_code=404, detail="Player not found")

        metrics_rows = cur.execute(
            """
            SELECT
                COALESCE(date, recorded_at, substr(created_at, 1, 10)) AS date,
                exit_velocity, launch_angle, spin_rate
            FROM metrics
            WHERE player_id = ?
            ORDER BY COALESCE(date, recorded_at, created_at) DESC, id DESC
            LIMIT 25
            """,
            (player_id,),
        ).fetchall()

        notes = cur.execute(
            """
            SELECT text, shared, created_at
            FROM notes
            WHERE player_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 25
            """,
            (player_id,),
        ).fetchall()

        drills = cur.execute(
            "SELECT id, title FROM drills ORDER BY title"
        ).fetchall()

        # --- chart arrays (oldest->newest for nicer charts) ---
        def _label(v):
            if isinstance(v, datetime):
                return v.date().isoformat()
            if isinstance(v, date):
                return v.isoformat()
            if v is None:
                return ""
            s = str(v)
            # trim to YYYY-MM-DD if a timestamp came back
            return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s

        dates, exitv, launch, spin = [], [], [], []
        for m in reversed(list(metrics_rows)):  # reverse DESC -> chronological
            # sqlite3.Row or tuple-safe access
            row = m
            get = (lambda k: row[k]) if isinstance(row, dict) or hasattr(row, "keys") else (lambda k: row[0])
            d = row["date"] if isinstance(row, sqlite3.Row) or hasattr(row, "keys") else m[0]

            dates.append(_label(d))
            exitv.append(float((row["exit_velocity"] if hasattr(row, "keys") else m[1]) or 0))
            launch.append(float((row["launch_angle"]  if hasattr(row, "keys") else m[2]) or 0))
            spin.append(float((row["spin_rate"]     if hasattr(row, "keys") else m[3]) or 0))

        ctx = {
            "request": request,
            "player": player,
            "metrics": metrics_rows,
            "notes": notes,
            "drills": drills,
            # chart data for template <script> using |tojson
            "dates": dates,
            "exitv": exitv,
            "launch": launch,
            "spin": spin,
        }
        return templates.TemplateResponse("instructor_player_detail.html", ctx)
    finally:
        conn.close()


@app.post("/metrics/add")
def add_metrics(
    request: Request,
    player_id: int = Form(...),
    date_str: str | None = Form(None, alias="date"),
    exit_velocity: float | None = Form(None),
    launch_angle: float | None = Form(None),
    spin_rate: float | None = Form(None),
):
    try:
        _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    dval = (date_str or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO metrics (player_id, date, exit_velocity, launch_angle, spin_rate, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (player_id, dval, exit_velocity, launch_angle, spin_rate),
        )
        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

@app.post("/notes/add")
def add_note(
    request: Request,
    player_id: int = Form(...),
    text: str = Form(...),
    share_with_player: str | None = Form(None),
    text_player: str | None = Form(None),
):
    try:
        iid = _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text required")

    shared = 1 if share_with_player else 0

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO notes (player_id, instructor_id, text, shared, kind, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'coach', datetime('now'), datetime('now'))
            """,
            (player_id, iid, text, shared),
        )
        conn.commit()
        # TODO: if text_player, trigger SMS integration here.
    finally:
        conn.close()

    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

@app.post("/drills/assign")
def assign_drill(
    request: Request,
    player_id: int = Form(...),
    drill_id: int = Form(...),
    note: str | None = Form(None),
):
    try:
        iid = _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO drill_assignments (player_id, instructor_id, drill_id, note, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'assigned', datetime('now'), datetime('now'))
            """,
            (player_id, iid, drill_id, (note or "").strip()),
        )
        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

# -----------------------------------------------------------------------------
# Player dashboard
# -----------------------------------------------------------------------------
# at top with other imports
import sqlite3
from datetime import date, datetime

def _years_old(dob_str: str | None) -> int | None:
    if not dob_str:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            d = datetime.strptime(dob_str, fmt).date()
            today = date.today()
            return today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        except ValueError:
            continue
    return None

@app.get("/dashboard")
def dashboard(request: Request):
    pid = request.session.get("player_id")
    if not pid:
        return RedirectResponse("/", status_code=303)

    conn = get_db()
    try:
        # Pull everything, then compute fallbacks in Python to avoid missing-column errors
        row = conn.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()
        if not row:
            return RedirectResponse("/", status_code=303)

        player = dict(row)
        player["avatar_url"] = player.get("avatar_url") or player.get("image_path") or None
        login_code = player.get("login_code") or ""

        # Age from whichever DOB column exists
        dob_str = player.get("birthdate") or player.get("dob") or player.get("date_of_birth")
        age_years = _years_old(dob_str)

        # Chart data (safe)
        mrows = conn.execute(
            """
            SELECT COALESCE(date, recorded_at, substr(created_at,1,10)) AS d,
                   exit_velocity
            FROM metrics
            WHERE player_id = ? AND exit_velocity IS NOT NULL
            ORDER BY COALESCE(date, recorded_at, created_at) ASC, id ASC
            LIMIT 90
            """,
            (pid,),
        ).fetchall()
        dates = [r["d"] for r in mrows] if mrows else []
        exitv  = [float(r["exit_velocity"]) for r in mrows] if mrows else []

        # Notes: select all, then filter in Python for shared flags (handles schemas w/ or w/o share_with_player)
        nrows = conn.execute(
            """
            SELECT * FROM notes
            WHERE player_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 100
            """,
            (pid,),
        ).fetchall()
        notes = []
        for r in nrows or []:
            d = dict(r)
            if bool(d.get("shared")) or bool(d.get("share_with_player")):
                notes.append(d)

        # Assignments: try join, but tolerate missing tables/columns
        try:
            arows = conn.execute(
                """
                SELECT a.*,
                       COALESCE(d.title, d.name, 'Drill') AS drill_name
                FROM assignments a
                LEFT JOIN drills d ON d.id = a.drill_id
                WHERE a.player_id = ?
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 25
                """,
                (pid,),
            ).fetchall()
        except sqlite3.OperationalError:
            arows = []
        assignments = [dict(r) for r in (arows or [])]

        ctx = {
            "request": request,
            "player": player,          # dict works with dot-access in Jinja
            "age_years": age_years,
            "login_code": login_code,
            "dates": dates,
            "exitv": exitv,
            "notes": notes,
            "assignments": assignments,
        }
        return templates.TemplateResponse("dashboard.html", ctx)
    finally:
        conn.close()
