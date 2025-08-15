# app/main.py
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Tuple

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# --------------------------------------------------------------------------------------
# Config / Paths
# --------------------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'app.db').as_posix()}")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

# --------------------------------------------------------------------------------------
# App / DB
# --------------------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# static files (logo, css, js)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --------------------------------------------------------------------------------------
# Jinja helpers
# --------------------------------------------------------------------------------------
def _datetimeformat(value: Any, fmt: str = "%Y-%m-%d") -> str:
    if not value:
        return ""
    try:
        if isinstance(value, str):
            # try ISO string
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value
        else:
            dt = value  # assume datetime-like
        return dt.strftime(fmt)
    except Exception:
        return str(value)

def _initials(name: str) -> str:
    if not name:
        return ""
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

templates.env.filters["datetimeformat"] = _datetimeformat
templates.env.filters["initials"] = _initials

# --------------------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------------------
def _table_has_column(db, table: str, column: str) -> bool:
    res = db.execute(text(f"PRAGMA table_info({table})"))
    for row in res:
        if row[1] == column:
            return True
    return False

def ensure_schema() -> None:
    """Create tables if they don't exist and add any missing columns we rely on."""
    db = SessionLocal()
    try:
        # players
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    login_code TEXT,
                    avatar_url TEXT,
                    instructor_id INTEGER
                )
                """
            )
        )

        # instructors (minimal)
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS instructors (
                    id INTEGER PRIMARY KEY,
                    name TEXT
                )
                """
            )
        )

        # favorites (coach "stars" a player)
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    id INTEGER PRIMARY KEY,
                    instructor_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

        # drills, drill_assignments (minimal)
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS drills (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    description TEXT
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS drill_assignments (
                    id INTEGER PRIMARY KEY,
                    player_id INTEGER NOT NULL,
                    instructor_id INTEGER,
                    drill_id INTEGER,
                    note TEXT,
                    status TEXT DEFAULT 'assigned',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        # add due_date if missing
        if not _table_has_column(db, "drill_assignments", "due_date"):
            db.execute(text("ALTER TABLE drill_assignments ADD COLUMN due_date DATETIME"))
        
        # notes (coach notes to player)
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY,
                    player_id INTEGER NOT NULL,
                    instructor_id INTEGER,
                    text TEXT,
                    shared INTEGER DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        if not _table_has_column(db, "notes", "kind"):
            db.execute(text("ALTER TABLE notes ADD COLUMN kind TEXT DEFAULT 'coach'"))

        # metrics (time series values like exit velocity)
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY,
                    player_id INTEGER NOT NULL,
                    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        for col, ddl in [
            ("metric", "TEXT"),
            ("value", "REAL"),
            ("unit", "TEXT"),
            ("source", "TEXT"),
            ("entered_by_instructor_id", "INTEGER"),
            ("note", "TEXT"),
        ]:
            if not _table_has_column(db, "metrics", col):
                db.execute(text(f"ALTER TABLE metrics ADD COLUMN {col} {ddl}"))

        # backfill recorded_at if needed
        db.execute(text("UPDATE metrics SET recorded_at = COALESCE(recorded_at, CURRENT_TIMESTAMP)"))

        db.commit()
    finally:
        db.close()

@app.on_event("startup")
def _startup() -> None:
    ensure_schema()

# --------------------------------------------------------------------------------------
# Query helpers used by routes
# --------------------------------------------------------------------------------------
def get_ev_series(db, player_id: int) -> Tuple[List[str], List[float]]:
    rows = db.execute(
        text(
            """
            SELECT recorded_at, value
            FROM metrics
            WHERE player_id = :pid AND metric = 'exit_velocity'
            ORDER BY recorded_at ASC
            """
        ),
        {"pid": player_id},
    ).all()
    dates = []
    values = []
    for r in rows:
        dt = r[0]
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except Exception:
                pass
        try:
            dates.append(dt.strftime("%Y-%m-%d"))
        except Exception:
            dates.append(str(dt))
        values.append(r[1] if r[1] is not None else 0.0)
    return dates, values

def get_latest_metrics(db, player_id: int, limit: int = 10) -> List[dict]:
    rows = db.execute(
        text(
            """
            SELECT id, player_id, metric, value, unit, recorded_at, source, entered_by_instructor_id, note
            FROM metrics
            WHERE player_id = :pid
            ORDER BY recorded_at DESC
            LIMIT :lim
            """
        ),
        {"pid": player_id, "lim": limit},
    ).mappings().all()
    return [dict(r) for r in rows]

def get_instructor_players(db, instructor_id: int) -> List[dict]:
    rows = db.execute(
        text(
            """
            SELECT id, name, login_code, avatar_url
            FROM players
            WHERE COALESCE(instructor_id, 0) = :iid OR :iid = 1  -- simple demo: coach 1 sees all
            ORDER BY id ASC
            """
        ),
        {"iid": instructor_id},
    ).mappings().all()
    return [dict(r) for r in rows]

def get_player(db, player_id: int) -> dict | None:
    r = db.execute(
        text("SELECT id, name, login_code, avatar_url FROM players WHERE id = :pid"),
        {"pid": player_id},
    ).mappings().first()
    return dict(r) if r else None

def is_favorite(db, instructor_id: int, player_id: int) -> bool:
    r = db.execute(
        text(
            "SELECT 1 FROM favorites WHERE instructor_id = :iid AND player_id = :pid LIMIT 1"
        ),
        {"iid": instructor_id, "pid": player_id},
    ).first()
    return bool(r)

def count_player_sessions(db, player_id: int) -> int:
    # "sessions" as a proxy = metric entries count
    r = db.execute(
        text("SELECT COUNT(1) FROM metrics WHERE player_id = :pid"),
        {"pid": player_id},
    ).first()
    return int(r[0]) if r and r[0] is not None else 0

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# --- Auth-ish (very light demo-style) ---
@app.post("/login/instructor")
def login_instructor(request: Request, name: str = Form("Coach"), instructor_id: int = Form(1)):
    request.session["user"] = {"role": "instructor", "id": int(instructor_id), "name": name}
    return RedirectResponse("/instructor", status_code=303)

@app.post("/login/player")
def login_player(request: Request, code: str = Form(...)):
    db = SessionLocal()
    try:
        r = db.execute(
            text("SELECT id, name, login_code FROM players WHERE login_code = :code"),
            {"code": code.strip()},
        ).mappings().first()
    finally:
        db.close()
    if not r:
        return RedirectResponse("/", status_code=303)
    request.session["user"] = {"role": "player", "player_id": r["id"], "name": r["name"], "login_code": r["login_code"]}
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

# --- Instructor screens ---
@app.get("/instructor")
def instructor_home(request: Request):
    user = request.session.get("user")
    if not user or user.get("role") != "instructor":
        return RedirectResponse("/", status_code=303)

    iid = int(user.get("id", 1))
    db = SessionLocal()
    try:
        players = get_instructor_players(db, iid)

        # Build array of (player_dict, sessions_count, is_fav_bool)
        arr: List[Tuple[dict, int, bool]] = []
        for p in players:
            sess = count_player_sessions(db, p["id"])
            fav = is_favorite(db, iid, p["id"])
            arr.append((p, sess, fav))

        ctx = {
            "request": request,
            "user": user,
            "arr": arr,
        }
    finally:
        db.close()

    return templates.TemplateResponse("instructor_dashboard.html", ctx)

@app.get("/instructor/player/{player_id}")
def instructor_player_detail(request: Request, player_id: int):
    user = request.session.get("user")
    if not user or user.get("role") != "instructor":
        return RedirectResponse("/", status_code=303)

    db = SessionLocal()
    try:
        player = get_player(db, player_id)
        dates, exitv = get_ev_series(db, player_id)
        latest_metrics = get_latest_metrics(db, player_id, limit=10)

        # drills (minimal; safe if empty)
        assignments = db.execute(
            text(
                """
                SELECT da.id, da.player_id, da.drill_id, da.status, da.note, da.created_at, da.updated_at, da.due_date,
                       d.name AS drill_name
                FROM drill_assignments da
                LEFT JOIN drills d ON d.id = da.drill_id
                WHERE da.player_id = :pid
                ORDER BY da.created_at DESC
                """
            ),
            {"pid": player_id},
        ).mappings().all()

        # coach notes (shared ones)
        notes = db.execute(
            text(
                """
                SELECT id, text, kind, created_at
                FROM notes
                WHERE player_id = :pid AND shared = 1
                ORDER BY created_at DESC
                """
            ),
            {"pid": player_id},
        ).mappings().all()

        ctx = {
            "request": request,
            "user": user,
            "player": player,
            "player_id": player_id,
            "dates": dates,
            "exitv": exitv,
            "latest_metrics": latest_metrics,
            "assignments": assignments,
            "notes": notes,
        }
    finally:
        db.close()

    return templates.TemplateResponse("instructor_player_detail.html", ctx)

@app.post("/players/create")
def create_player(request: Request, name: str = Form(...)):
    user = request.session.get("user")
    if not user or user.get("role") != "instructor":
        return RedirectResponse("/", status_code=303)

    # simple login code generator
    code = "".join([c for c in (name.upper().replace(" ", "") + "XXXX")][:6])
    code = code[:3] + os.urandom(2).hex().upper()[:3]  # mix in randomness

    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO players (name, login_code, instructor_id) VALUES (:n, :c, :iid)"
            ),
            {"n": name.strip(), "c": code, "iid": int(user.get("id", 1))},
        )
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/instructor", status_code=303)

@app.post("/favorite/{player_id}")
def toggle_favorite(request: Request, player_id: int):
    user = request.session.get("user")
    if not user or user.get("role") != "instructor":
        return JSONResponse({"ok": False, "error": "not_authorized"}, status_code=401)

    iid = int(user.get("id", 1))
    db = SessionLocal()
    try:
        exists = db.execute(
            text(
                "SELECT id FROM favorites WHERE instructor_id = :iid AND player_id = :pid LIMIT 1"
            ),
            {"iid": iid, "pid": player_id},
        ).first()
        if exists:
            db.execute(text("DELETE FROM favorites WHERE id = :id"), {"id": exists[0]})
            db.commit()
            return JSONResponse({"ok": True, "favorite": False})
        else:
            db.execute(
                text(
                    "INSERT INTO favorites (instructor_id, player_id) VALUES (:iid, :pid)"
                ),
                {"iid": iid, "pid": player_id},
            )
            db.commit()
            return JSONResponse({"ok": True, "favorite": True})
    finally:
        db.close()

# --- Metrics ---
@app.post("/metrics/add")
def add_metric(
    request: Request,
    player_id: int = Form(...),
    metric: str = Form(...),
    value: float = Form(...),
    unit: str | None = Form(None),
    note: str | None = Form(None),
):
    user = request.session.get("user")
    if not user or user.get("role") != "instructor":
        return RedirectResponse("/", status_code=303)

    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                INSERT INTO metrics
                    (player_id, metric, value, unit, recorded_at, source, entered_by_instructor_id, note)
                VALUES
                    (:player_id, :metric, :value, :unit, :recorded_at, :source, :entered_by, :note)
                """
            ),
            {
                "player_id": player_id,
                "metric": metric,
                "value": value,
                "unit": unit,
                "recorded_at": datetime.now(timezone.utc),
                "source": "manual",
                "entered_by": int(user.get("id", 1)),
                "note": note,
            },
        )
        db.commit()
    finally:
        db.close()

    return RedirectResponse(f"/instructor/player/{player_id}", status_code=303)

# --- Player dashboard ---
@app.get("/dashboard")
def dashboard(request: Request):
    user = request.session.get("user")
    if not user or user.get("role") != "player":
        return RedirectResponse("/", status_code=303)

    db = SessionLocal()
    try:
        pid = int(user["player_id"])
        player = get_player(db, pid)
        dates, exitv = get_ev_series(db, pid)
        latest_metrics = get_latest_metrics(db, pid, limit=10)

        # most recent shared note (if any)
        last_note = db.execute(
            text(
                """
                SELECT id, text, kind, created_at
                FROM notes
                WHERE player_id = :pid AND shared = 1
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"pid": pid},
        ).mappings().first()

        # current assignments
        assignments = db.execute(
            text(
                """
                SELECT da.id, da.status, da.note, da.due_date, da.created_at,
                       d.name AS drill_name
                FROM drill_assignments da
                LEFT JOIN drills d ON d.id = da.drill_id
                WHERE da.player_id = :pid
                ORDER BY da.created_at DESC
                """
            ),
            {"pid": pid},
        ).mappings().all()

        ctx = {
            "request": request,
            "user": user,
            "player": player,
            "dates": dates or [],              # important for Jinja tojson
            "exitv": exitv or [],
            "latest_metrics": latest_metrics or [],
            "last_note": dict(last_note) if last_note else None,
            "assignments": assignments or [],
        }
    finally:
        db.close()

    return templates.TemplateResponse("dashboard.html", ctx)

# --------------------------------------------------------------------------------------
# Uvicorn entrypoint (for local runs)
# --------------------------------------------------------------------------------------
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
