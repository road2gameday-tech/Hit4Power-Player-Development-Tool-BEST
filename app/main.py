# app/main.py
from __future__ import annotations

import os
import random
import sqlite3
import string
from datetime import datetime, date
from typing import Dict, List, Tuple, Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse, Response
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

# -----------------------------------------------------------------------------
# Jinja filters
# -----------------------------------------------------------------------------
def _datetimeformat(value, fmt="%Y-%m-%d"):
    """Format a datetime/date/ISO string for display."""
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

def _initials(value: str, max_letters: int = 2) -> str:
    """'John Q Public' -> 'JQ'."""
    s = str(value or "").strip()
    if not s:
        return ""
    parts = [p for p in s.split() if p]
    letters = "".join(p[0] for p in parts[:max_letters])
    return letters.upper()

templates.env.filters["datetimeformat"] = _datetimeformat
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

def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cur.fetchall())

def ensure_schema():
    """Create tables and add any missing columns referenced by templates/routes."""
    conn = get_db()
    try:
        # players
        conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                login_code TEXT UNIQUE,
                image_path TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)

        # notes
        conn.execute("""
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
            );
        """)
        if not _has_column(conn, "notes", "kind"):
            conn.execute("ALTER TABLE notes ADD COLUMN kind TEXT DEFAULT 'coach'")

        # drills
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)

        # drill_assignments (canonical)
        conn.execute("""
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
            );
        """)
        if not _has_column(conn, "drill_assignments", "due_date"):
            conn.execute("ALTER TABLE drill_assignments ADD COLUMN due_date TEXT")

        # metrics
        conn.execute("""
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
            );
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

        # favorites (per-instructor)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS instructor_favorites (
                instructor_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (instructor_id, player_id),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );
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
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"not ready: {e}")

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

# -----------------------------------------------------------------------------
# Session helpers
# -----------------------------------------------------------------------------
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
# Drill library & assignment
# -----------------------------------------------------------------------------
@app.get("/drills")
def drill_library(request: Request, player_id: Optional[int] = None, q: Optional[str] = None):
    # Instructor-only drill library. If player_id is provided, show "Assign" buttons in template.
    try:
        _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    pattern = f"%{q}%" if q else "%"
    conn = get_db()
    try:
        drills = conn.execute(
            "SELECT id, title, COALESCE(description, '') AS description FROM drills WHERE title LIKE ? ORDER BY title",
            (pattern,),
        ).fetchall()

        ctx = {"request": request, "drills": drills, "player_id": player_id, "q": q or ""}
        return templates.TemplateResponse("drill_library.html", ctx)
    finally:
        conn.close()

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
            (player_id, iid, drill_id, (note or "").strip() or None),
        )
        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

# -----------------------------------------------------------------------------
# Instructor dashboard & actions
# -----------------------------------------------------------------------------
@app.get("/instructor")
def instructor_home(request: Request):
    # must be logged in as instructor
    try:
        _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    conn = get_db()
    try:
        instructor_id = request.session.get("instructor_id")
        view = request.query_params.get("filter", "all")  # "all" | "favorites"

        # Pull all players and annotate favorite status using the correct table
        all_players = conn.execute(
            """
            SELECT p.*,
                   EXISTS (
                       SELECT 1
                       FROM instructor_favorites f
                       WHERE f.player_id = p.id
                         AND f.instructor_id = ?
                   ) AS is_favorite
            FROM players p
            ORDER BY p.name
            """,
            (instructor_id,),
        ).fetchall()

        # Slice out favorites for “My Clients”
        fav_players = [r for r in all_players if r["is_favorite"]]

        # Build the buckets the template loops over: grouped.items()
        if view == "favorites":
            grouped = {"My Clients": fav_players}
            players = fav_players
        else:
            grouped = {}
            if fav_players:
                grouped["My Clients"] = fav_players
            grouped["All Players"] = all_players
            players = all_players

        ctx = {
            "request": request,
            "players": players,   # some templates may use this
            "grouped": grouped,   # instructor_dashboard.html expects this
            "filter": view,
        }
        return templates.TemplateResponse("instructor_dashboard.html", ctx)
    finally:
        conn.close()


# Convenience route so "My Clients" can point here directly
@app.get("/instructor/clients")
def instructor_clients_redirect():
    return RedirectResponse("/instructor?filter=favorites", status_code=303)

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
        return JSONResponse({"ok": False, "favorite": False, "favorited": False}, status_code=401)

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
            return JSONResponse({"ok": True, "favorite": False, "favorited": False})
        else:
            conn.execute(
                "INSERT INTO instructor_favorites (instructor_id, player_id) VALUES (?, ?)",
                (iid, player_id),
            )
            conn.commit()
            return JSONResponse({"ok": True, "favorite": True, "favorited": True})
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
        cur = conn.cursor()

        # --- player ---
        player_row = cur.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        if not player_row:
            raise HTTPException(status_code=404, detail="Player not found")
        player = dict(player_row)
        # Provide avatar_url convenience for templates
        player["avatar_url"] = player.get("avatar_url") or player.get("image_path") or None

        # --- metrics for chart (EV/LA/SR) ---
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

        def _label(v):
            if isinstance(v, datetime):
                return v.date().isoformat()
            if isinstance(v, date):
                return v.isoformat()
            if v is None:
                return ""
            s = str(v)
            return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s

        dates, exitv, launch, spin = [], [], [], []
        for m in reversed(list(metrics_rows)):  # reverse DESC -> chronological
            d = m["date"]
            dates.append(_label(d))
            exitv.append(float(m["exit_velocity"] or 0))
            launch.append(float(m["launch_angle"] or 0))
            spin.append(float(m["spin_rate"] or 0))

        # --- latest generic metrics list (for "Updated Metrics" section) ---
        latest_metrics = cur.execute(
            """
            SELECT metric, value, unit, source, note,
                   COALESCE(recorded_at, date, created_at) AS recorded_at
            FROM metrics
            WHERE player_id = ? AND metric IS NOT NULL
            ORDER BY COALESCE(recorded_at, date, created_at) DESC, id DESC
            LIMIT 25
            """,
            (player_id,),
        ).fetchall()

        # --- notes ---
        notes = cur.execute(
            """
            SELECT text, shared, kind, created_at
            FROM notes
            WHERE player_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 25
            """,
            (player_id,),
        ).fetchall()

        # --- drill library (for select dropdown) ---
        drills = cur.execute("SELECT id, title FROM drills ORDER BY title").fetchall()

        # --- current assignments (read-only list) ---
        assignments = cur.execute(
            """
            SELECT a.*,
                   COALESCE(d.title, 'Drill') AS drill_name
            FROM drill_assignments a
            LEFT JOIN drills d ON d.id = a.drill_id
            WHERE a.player_id = ?
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT 25
            """,
            (player_id,),
        ).fetchall()

        ctx = {
            "request": request,
            "player": player,
            "metrics": metrics_rows,
            "latest_metrics": latest_metrics,
            "notes": notes,
            "drills": drills,
            "assignments": assignments,
            # chart data for template <script> using |tojson
            "dates": dates,
            "exitv": exitv,
            "launch": launch,
            "spin": spin,
        }
        return templates.TemplateResponse("instructor_player_detail.html", ctx)
    finally:
        conn.close()

# -----------------------------------------------------------------------------
# Metrics & Notes (instructor actions)
# -----------------------------------------------------------------------------
@app.post("/metrics/add")
def add_metrics(
    request: Request,
    player_id: int = Form(...),
    # New generic style:
    metric: str | None = Form(None),
    value: float | None = Form(None),
    unit: str | None = Form(None),
    note: str | None = Form(None),
    # Old style (optional):
    date_str: str | None = Form(None, alias="date"),
    exit_velocity: float | None = Form(None),
    launch_angle: float | None = Form(None),
    spin_rate: float | None = Form(None),
):
    try:
        iid = _require_instructor(request)
    except HTTPException:
        return RedirectResponse("/", status_code=303)

    dval = (date_str or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_db()
    try:
        if metric and value is not None:
            # Insert generic metric row
            conn.execute(
                """
                INSERT INTO metrics (player_id, date, metric, value, unit, source, note, entered_by_instructor_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'manual', ?, ?, datetime('now'), datetime('now'))
                """,
                (player_id, dval, metric.strip(), float(value), (unit or "").strip() or None, (note or "").strip() or None, iid),
            )
        else:
            # Back-compat fields (EV/LA/SR)
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
    text_player: str | None = Form(None),  # placeholder flag
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

# -----------------------------------------------------------------------------
# Player dashboard
# -----------------------------------------------------------------------------
def _years_old(dob_str: str | None) -> Optional[int]:
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
        dates = [r["d"] for r in (mrows or [])]
        exitv  = [float(r["exit_velocity"]) for r in (mrows or [])]

        # Notes: pick shared ones
        nrows = conn.execute(
            """
            SELECT text, shared, kind, created_at
            FROM notes
            WHERE player_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 100
            """,
            (pid,),
        ).fetchall()
        notes = [dict(r) for r in (nrows or []) if bool(r["shared"])]

        # Assignments (read-only)
        arows = conn.execute(
            """
            SELECT a.*,
                   COALESCE(d.title, 'Drill') AS drill_name
            FROM drill_assignments a
            LEFT JOIN drills d ON d.id = a.drill_id
            WHERE a.player_id = ?
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT 25
            """,
            (pid,),
        ).fetchall()
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
