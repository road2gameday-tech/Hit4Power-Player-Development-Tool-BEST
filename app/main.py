# app/main.py
import os
import random
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Set

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette import status

from sqlalchemy import func, select, and_, text, inspect
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, ProgrammingError

from .database import SessionLocal, engine, Base
from .models import Player, Instructor, Metric, Note, DrillAssignment
from .utils import normalize_code, hash_code, set_flash, pop_flash, age_bucket

from collections import defaultdict
from datetime import datetime

def _chart_series(db: Session, player_id: int):
    """
    Build simple timeseries for common hitting metrics.
    Returns (dates[], ev[], la[], sr[]), where dates are 'YYYY-MM-DD' strings.
    Missing metrics -> empty lists.
    """
    rows = db.execute(
        select(Metric)
        .where(Metric.player_id == player_id)
        .order_by(Metric.recorded_at.asc())
    ).scalars().all()

    if not rows:
        return [], [], [], []

    # Collect unique day labels in order
    def _day(v):
        if isinstance(v, datetime):
            return v.date().isoformat()
        # strings like "2025-01-05 12:34" or "2025-01-05"
        s = str(v).strip()
        return s[:10] if len(s) >= 10 else s

    labels = []
    seen = set()
    for r in rows:
        d = _day(r.recorded_at)
        if d not in seen:
            labels.append(d)
            seen.add(d)

    # Map date -> value for each metric we care about
    wanted = {
        "exit_velocity": defaultdict(lambda: None),
        "launch_angle": defaultdict(lambda: None),
        "spin_rate": defaultdict(lambda: None),
    }
    for r in rows:
        m = (r.metric or "").strip().lower()
        if m in wanted:
            wanted[m][_day(r.recorded_at)] = r.value

    ev = [wanted["exit_velocity"][d] for d in labels]
    la = [wanted["launch_angle"][d] for d in labels]
    sr = [wanted["spin_rate"][d] for d in labels]
    return labels, ev, la, sr


# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------
app = FastAPI()

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-insecure-session-secret")
HTTPS_ONLY = os.getenv("ENV", "dev") != "dev"  # set ENV=prod on Render for secure cookies
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=HTTPS_ONLY,
    session_cookie="h4p_session",
)

# Static & templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

from datetime import datetime, timezone

def _parse_any_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    if not s:
        return None
    # support trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        # last-ditch common format
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
    return None

def _datetimeformat_filter(value, fmt="%b %d, %Y"):
    dt = _parse_any_dt(value)
    return dt.strftime(fmt) if dt else ""

def _ago_filter(value):
    dt = _parse_any_dt(value)
    if not dt:
        return ""
    now = datetime.now(dt.tzinfo or timezone.utc) if dt.tzinfo else datetime.utcnow()
    seconds = int((now - dt).total_seconds())
    for name, secs in (("year", 31536000), ("month", 2592000), ("week", 604800),
                       ("day", 86400), ("hour", 3600), ("minute", 60)):
        n = seconds // secs
        if n >= 1:
            return f"{n} {name}{'' if n == 1 else 's'} ago"
    return "just now"

# Register filters for all templates
templates.env.filters["datetimeformat"] = _datetimeformat_filter
templates.env.filters["ago"] = _ago_filter


# Create missing **tables** (not columns)
Base.metadata.create_all(bind=engine)

# ------------------------------------------------------------------------------
# Schema helpers / patching
# ------------------------------------------------------------------------------
def _has_column(table: str, col: str) -> bool:
    try:
        cols = {c["name"] for c in inspect(engine).get_columns(table)}
        return col in cols
    except Exception as e:
        print(f"[has_column] cannot inspect {table}: {e}")
        return False

def _has_table(table: str) -> bool:
    try:
        return inspect(engine).has_table(table)
    except Exception as e:
        print(f"[has_table] cannot inspect {table}: {e}")
        return False

def _ensure_schema():
    """Add legacy columns if missing (idempotent) and backfill needed data."""
    insp = inspect(engine)

    def cols(table: str) -> set[str]:
        try:
            return {c["name"] for c in insp.get_columns(table)}
        except Exception as e:
            print(f"[ensure_schema] cannot inspect {table}: {e}")
            return set()

    instr_cols = cols("instructors")
    player_cols = cols("players")
    metric_cols = cols("metrics")
    notes_cols  = cols("notes")
    drills_cols = cols("drill_assignments")

    dialect = engine.dialect.name

    def _add(table: str, ddl: str):
        # Postgres supports IF NOT EXISTS; SQLite doesn't (we guard via cols())
        stmt = ddl if dialect != "postgresql" else ddl.replace(" ADD COLUMN ", " ADD COLUMN IF NOT EXISTS ")
        with engine.begin() as conn:
            conn.execute(text(stmt))
        print(f"[ensure_schema] {table}: applied -> {ddl}")

    # Instructors
    if "login_code" not in instr_cols:
        _add("instructors", "ALTER TABLE instructors ADD COLUMN login_code TEXT")
    if "created_at" not in instr_cols:
        _add("instructors", "ALTER TABLE instructors ADD COLUMN created_at TEXT")
    if "updated_at" not in instr_cols:
        _add("instructors", "ALTER TABLE instructors ADD COLUMN updated_at TEXT")

    # Players (ensure columns used by templates / models)
    if "login_code" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN login_code TEXT")
    if "age" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN age INTEGER")
    if "phone" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN phone TEXT")
    if "image_path" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN image_path TEXT")
    if "created_at" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN created_at TEXT")
    if "updated_at" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN updated_at TEXT")

    # Metrics — ensure the full set of columns your models/templates expect
    expected_metrics_cols = [
        ("metric", "TEXT"),
        ("value", "REAL"),
        ("unit", "TEXT"),
        ("recorded_at", "TEXT"),
        ("source", "TEXT"),
        ("entered_by_instructor_id", "INTEGER"),
        ("note", "TEXT"),
    ]
    for name, typ in expected_metrics_cols:
        if name not in metric_cols:
            _add("metrics", f"ALTER TABLE metrics ADD COLUMN {name} {typ}")

    # Backfill recorded_at from created_at if present; else current time
    metric_cols_after = cols("metrics")
    if "recorded_at" in metric_cols_after:
        with engine.begin() as conn:
            if "created_at" in metric_cols_after:
                conn.execute(text(
                    "UPDATE metrics SET recorded_at = COALESCE(recorded_at, created_at, CURRENT_TIMESTAMP)"
                ))
            else:
                conn.execute(text(
                    "UPDATE metrics SET recorded_at = COALESCE(recorded_at, CURRENT_TIMESTAMP)"
                ))
        print("[ensure_schema] metrics: backfilled recorded_at")

    # Notes — ensure kind/shared/timestamps exist so dashboard query won't 500
    if "kind" not in notes_cols:
        _add("notes", "ALTER TABLE notes ADD COLUMN kind TEXT")
    if "shared" not in notes_cols:
        _add("notes", "ALTER TABLE notes ADD COLUMN shared INTEGER")
    if "created_at" not in notes_cols:
        _add("notes", "ALTER TABLE notes ADD COLUMN created_at TEXT")
    if "updated_at" not in notes_cols:
        _add("notes", "ALTER TABLE notes ADD COLUMN updated_at TEXT")
    with engine.begin() as conn:
        conn.execute(text("UPDATE notes SET shared = COALESCE(shared, 1)"))
        conn.execute(text("UPDATE notes SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE notes SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"))

    # Drill assignments — make sure fields used in queries exist
    drills_cols = cols("drill_assignments")
    if "status" not in drills_cols:
        _add("drill_assignments", "ALTER TABLE drill_assignments ADD COLUMN status TEXT")
    if "note" not in drills_cols:
        _add("drill_assignments", "ALTER TABLE drill_assignments ADD COLUMN note TEXT")
    if "due_date" not in drills_cols:
        _add("drill_assignments", "ALTER TABLE drill_assignments ADD COLUMN due_date TEXT")
    if "created_at" not in drills_cols:
        _add("drill_assignments", "ALTER TABLE drill_assignments ADD COLUMN created_at TEXT")
    if "updated_at" not in drills_cols:
        _add("drill_assignments", "ALTER TABLE drill_assignments ADD COLUMN updated_at TEXT")

    with engine.begin() as conn:
        conn.execute(text("UPDATE drill_assignments SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE drill_assignments SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE drill_assignments SET status = COALESCE(status, 'assigned')"))


    # Favorites mapping table (instructor <-> player)
    if not _has_table("favorites"):
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE favorites (
                    instructor_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    PRIMARY KEY (instructor_id, player_id)
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_favorites_instructor ON favorites (instructor_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_favorites_player ON favorites (player_id)"))
        print("[ensure_schema] created favorites table")

    # Safety backfill for timestamps (idempotent)
    with engine.begin() as conn:
        conn.execute(text("UPDATE instructors SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE instructors SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE players SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE players SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"))

# Run schema patch once at import time (service startup)
_ensure_schema()

# ------------------------------------------------------------------------------
# DB dependency
# ------------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def _login_lookup(db: Session, model, raw_code: str):
    """Try normalized plaintext, then hashed; swallow schema errors to avoid 500s."""
    norm = normalize_code(raw_code)
    try:
        obj = db.execute(select(model).where(model.login_code == norm)).scalar_one_or_none()
        if obj:
            return obj
        hashed = hash_code(raw_code)
        return db.execute(select(model).where(model.login_code == hashed)).scalar_one_or_none()
    except (OperationalError, ProgrammingError) as e:
        print(f"[login_lookup] schema issue: {e}")
        return None

def _latest_metrics_query(player_id: int):
    """Newest row per (player_id, metric) for dashboard 'Latest metrics'."""
    if _has_column("metrics", "recorded_at"):
        subq = (
            select(Metric.metric.label("metric"), func.max(Metric.recorded_at).label("latest_at"))
            .where(Metric.player_id == player_id)
            .group_by(Metric.metric)
            .subquery()
        )
        return (
            select(Metric)
            .where(Metric.player_id == player_id)
            .join(subq, and_(Metric.metric == subq.c.metric, Metric.recorded_at == subq.c.latest_at))
            .order_by(Metric.metric.asc())
        )

    subq = (
        select(Metric.metric.label("metric"), func.max(Metric.id).label("latest_id"))
        .where(Metric.player_id == player_id)
        .group_by(Metric.metric)
        .subquery()
    )
    return (
        select(Metric)
        .where(Metric.player_id == player_id)
        .join(subq, and_(Metric.metric == subq.c.metric, Metric.id == subq.c.latest_id))
        .order_by(Metric.metric.asc())
    )

def _parse_iso_dt(value: Optional[str]) -> datetime:
    """Parse ISO8601 timestamps including 'Z' suffix; default to now if empty/invalid."""
    if not value:
        return datetime.utcnow()
    v = value.strip()
    if v.endswith("Z"):
        v = v.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return datetime.utcnow()

def _parse_dt_from_db(val: Optional[str]) -> Optional[datetime]:
    """Parse DB timestamp-ish strings into datetime (lenient)."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        pass
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def _bucket_by_recency(dt: Optional[datetime]) -> str:
    if not dt:
        return "No data yet"
    now = datetime.utcnow()
    days = (now - dt).days
    if days <= 0:
        return "Updated today"
    if days <= 7:
        return "Updated this week"
    if days <= 30:
        return "Updated this month"
    return "Stale (30+ days)"

def _session_counts_by_player(db: Session) -> Dict[int, int]:
    """Count distinct metric days per player."""
    counts: Dict[int, int] = {}
    if not _has_column("metrics", "recorded_at"):
        rows = db.execute(select(Metric.player_id, func.count(Metric.id)).group_by(Metric.player_id)).all()
        for pid, c in rows:
            counts[int(pid)] = int(c)
        return counts

    rows = db.execute(
        select(
            Metric.player_id,
            func.count(func.distinct(func.date(Metric.recorded_at)))
        ).group_by(Metric.player_id)
    ).all()
    for pid, c in rows:
        counts[int(pid)] = int(c)
    return counts

def _player_to_dict(p: Player) -> Dict[str, Optional[str]]:
    """Convert ORM Player to a dict so Jinja dot access avoids ORM lazy-loads."""
    return {
        "id": p.id,
        "name": p.name,
        "age": p.age,
        "login_code": getattr(p, "login_code", None),
        "phone": getattr(p, "phone", None),
        "image_path": getattr(p, "image_path", None),
        "created_at": getattr(p, "created_at", None),
        "updated_at": getattr(p, "updated_at", None),
    }

def _get_favorites(db: Session, instructor_id: int) -> Set[int]:
    rows = db.execute(
        text("SELECT player_id FROM favorites WHERE instructor_id = :iid"),
        {"iid": instructor_id}
    ).all()
    return {int(r[0]) for r in rows}

def _group_players(
    rows: List[Tuple[Player, Optional[str]]],
    session_counts: Dict[int, int],
    fav_ids: Optional[Set[int]] = None
) -> Dict[str, List[Tuple[dict, int, bool]]]:
    """
    rows: sequence of (Player, last_metric_at)
    returns: dict bucket -> list of (player_dict_with_fake_metrics, sessions_int, is_fav_bool)
    """
    grouped: Dict[str, List[Tuple[dict, int, bool]]] = {}
    parsed: List[Tuple[Player, Optional[datetime]]] = []

    for p, last_raw in rows:
        last_dt = _parse_dt_from_db(last_raw)
        parsed.append((p, last_dt))
    parsed.sort(key=lambda x: ((x[1] or datetime(1970, 1, 1)), x[0].id), reverse=True)

    fav_ids = fav_ids or set()

    for p, last_dt in parsed:
        bucket = _bucket_by_recency(last_dt)
        sessions = session_counts.get(p.id, 0)
        is_fav = p.id in fav_ids
        pdict = _player_to_dict(p)
        # Provide a synthetic "metrics" list so template can call p.metrics|length without DB hits
        pdict["metrics"] = [None] * sessions
        grouped.setdefault(bucket, []).append((pdict, sessions, is_fav))

    return grouped

def _generate_unique_code(db: Session, length: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # avoid easily confused chars
    for _ in range(20):
        code = "".join(random.choice(alphabet) for _ in range(length))
        norm = normalize_code(code)
        exists = db.execute(select(Player).where(Player.login_code == norm)).scalar_one_or_none()
        if not exists:
            return code
    return "".join(random.choice(alphabet) for _ in range(length))

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.get("/")
def index(request: Request):
    ctx = {"request": request, "flash": pop_flash(request)}
    return templates.TemplateResponse("index.html", ctx)

@app.post("/login/player")
def login_player(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    player = _login_lookup(db, Player, code)
    if not player:
        set_flash(request, "Invalid code. Please try again.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    request.session["player_id"] = player.id
    request.session.pop("instructor_id", None)
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/login/instructor")
def login_instructor(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    # Try DB first
    coach = _login_lookup(db, Instructor, code)

    # Fallback to env var (INSTRUCTOR_DEFAULT_CODE)
    if not coach:
        env_code = os.getenv("INSTRUCTOR_DEFAULT_CODE", "")
        if env_code and normalize_code(code) == normalize_code(env_code):
            coach = db.execute(select(Instructor).order_by(Instructor.id.asc())).scalar_one_or_none()
            if not coach:
                coach = Instructor(name="Default Instructor", login_code=normalize_code(env_code))
                db.add(coach)
                db.commit()
            elif not coach.login_code:
                coach.login_code = normalize_code(env_code)
                db.commit()

    if not coach:
        set_flash(request, "Invalid code. Please try again.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    request.session["instructor_id"] = coach.id
    request.session.pop("player_id", None)
    return RedirectResponse(url="/instructor", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
def logout(request: Request):
    request.session.pop("player_id", None)
    request.session.pop("instructor_id", None)
    set_flash(request, "You have been logged out.")
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

# ------------------------- Player dashboard -----------------------------------
@app.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    pid = request.session.get("player_id")
    if not pid:
        set_flash(request, "Please log in first.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    player = db.get(Player, pid)
    if not player:
        set_flash(request, "Player not found.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    latest_metrics = db.execute(_latest_metrics_query(pid)).scalars().all()
    last_note = db.execute(
        select(Note)
        .where(Note.player_id == pid, Note.shared == True)  # noqa: E712
        .order_by(Note.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    drills = db.execute(
        select(DrillAssignment)
        .where(DrillAssignment.player_id == pid, DrillAssignment.status != "archived")
        .order_by(DrillAssignment.created_at.desc())
    ).scalars().all()
    
 # --- NEW: chart data so Jinja |tojson never sees Undefined
    dates, ev_series, la_series, sr_series = _chart_series(db, pid)
    # Provide multiple aliases to match whatever the template expects
    chart_ctx = {
        "dates": dates,
        "ev_series": ev_series, "la_series": la_series, "sr_series": sr_series,
        "ev": ev_series, "la": la_series, "sr": sr_series,
        "ev_values": ev_series, "la_values": la_series, "sr_values": sr_series,
        "values": ev_series,  # generic alias some templates use
    }
    # --- END NEW
    
    ctx = {
        "request": request,
        "flash": pop_flash(request),
        "player": player,
        "age_bucket": age_bucket(player.age),
        "latest_metrics": latest_metrics,
        "last_note": last_note,
        "drill_assignments": drills,
         **chart_ctx,  # NEW
    }
    return templates.TemplateResponse("dashboard.html", ctx)

# ------------------------- Instructor views -----------------------------------
@app.get("/instructor")
def instructor_home(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("instructor_id"):
        set_flash(request, "Please log in as an instructor.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    # Determine last metric time per player
    if _has_column("metrics", "recorded_at"):
        last_metric_subq = (
            select(
                Metric.player_id.label("pid"),
                func.max(Metric.recorded_at).label("last_metric_at"),
            )
            .group_by(Metric.player_id)
            .subquery()
        )
        rows = db.execute(
            select(Player, last_metric_subq.c.last_metric_at)
            .outerjoin(last_metric_subq, Player.id == last_metric_subq.c.pid)
            .order_by(
                func.coalesce(last_metric_subq.c.last_metric_at, text("'1970-01-01 00:00:00'")).desc(),
                Player.id.desc(),
            )
        ).all()
    else:
        rows = db.execute(
            select(Player, text("NULL as last_metric_at")).order_by(Player.id.desc())
        ).all()

    session_counts = _session_counts_by_player(db)
    fav_ids = _get_favorites(db, request.session["instructor_id"])
    grouped = _group_players(rows, session_counts, fav_ids=fav_ids)

    players = []
    for p, last_raw in rows:
        players.append({
            "player": _player_to_dict(p),
            "last_update": _parse_dt_from_db(last_raw),
        })

    ctx = {
        "request": request,
        "flash": pop_flash(request),
        "players": players,
        "grouped": grouped,   # used by instructor_dashboard.html
    }
    return templates.TemplateResponse("instructor_dashboard.html", ctx)

@app.get("/instructor/player/{player_id}")
def instructor_player_detail(player_id: int, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("instructor_id"):
        set_flash(request, "Please log in as an instructor.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    player = db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    latest_metrics = db.execute(_latest_metrics_query(player_id)).scalars().all()
    notes = db.execute(
        select(Note).where(Note.player_id == player_id).order_by(Note.created_at.desc()).limit(25)
    ).scalars().all()
    assignments = db.execute(
        select(DrillAssignment).where(DrillAssignment.player_id == player_id).order_by(DrillAssignment.created_at.desc())
    ).scalars().all()

    ctx = {
        "request": request,
        "flash": pop_flash(request),
        "player": player,
        "latest_metrics": latest_metrics,
        "notes": notes,
        "assignments": assignments,
    }
    return templates.TemplateResponse("instructor_player_detail.html", ctx)

# ------------------------- Instructor actions (writes) -------------------------

@app.post("/players/create")
async def create_player(
    request: Request,
    name: Optional[str] = Form(None),
    age: Optional[int] = Form(None),
    phone: Optional[str] = Form(None),
    login_code: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Creates a player.
    - Works for HTML form posts (Form(...))
    - Also accepts JSON payloads: {"name": "...", "age": 12, "phone": "...", "login_code": "..."}
    Redirects back to /instructor for HTML accepts; returns JSON otherwise.
    """
    if not request.session.get("instructor_id"):
        if "text/html" in (request.headers.get("accept") or ""):
            set_flash(request, "Please log in as an instructor.")
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # If this wasn't a form submit, try JSON body
    if name is None:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        name = payload.get("name")
        age = payload.get("age")
        phone = payload.get("phone")
        login_code = payload.get("login_code")

    if not name or not str(name).strip():
        return JSONResponse({"ok": False, "error": "name_required"}, status_code=400)

    # Determine a login code (normalize provided or generate unique)
    if login_code and str(login_code).strip():
        raw_code = str(login_code).strip()
    else:
        raw_code = _generate_unique_code(db)

    norm_code = normalize_code(raw_code)

    p = Player(
        name=str(name).strip(),
        age=age if (isinstance(age, int) or age is None) else None,
        phone=(str(phone).strip() or None) if phone is not None else None,
        login_code=norm_code,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    if "text/html" in (request.headers.get("accept") or ""):
        set_flash(request, f"Player '{p.name}' created. Code: {raw_code}")
        return RedirectResponse(url="/instructor", status_code=status.HTTP_303_SEE_OTHER)

    return JSONResponse({"ok": True, "player_id": p.id, "login_code": raw_code})

@app.post("/instructor/player/{player_id}/metric")
def add_metric(
    player_id: int,
    request: Request,
    metric: str = Form(...),
    value: float = Form(...),
    unit: Optional[str] = Form(None),
    recorded_at: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not request.session.get("instructor_id"):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    player = db.get(Player, player_id)
    if not player:
        return JSONResponse({"ok": False, "error": "player_not_found"}, status_code=404)

    when = _parse_iso_dt(recorded_at)
    m = Metric(
        player_id=player_id,
        metric=metric.strip().lower(),
        value=value,
        unit=(unit or "").strip().lower() or None,
        recorded_at=when,
        source="instructor",
        entered_by_instructor_id=request.session.get("instructor_id"),
    )
    db.add(m)
    db.commit()
    return JSONResponse({"ok": True})

@app.post("/instructor/player/{player_id}/note")
def add_note(
    player_id: int,
    request: Request,
    text: str = Form(...),
    shared: bool = Form(True),
    kind: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not request.session.get("instructor_id"):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    n = Note(
        player_id=player_id,
        instructor_id=request.session.get("instructor_id"),
        text=text.strip(),
        shared=bool(shared),
        kind=(kind or "").strip() or None,
    )
    db.add(n)
    db.commit()
    return JSONResponse({"ok": True})

@app.post("/instructor/player/{player_id}/assign_drill")
def assign_drill(
    player_id: int,
    request: Request,
    drill_id: int = Form(...),
    note: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not request.session.get("instructor_id"):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    a = DrillAssignment(
        player_id=player_id,
        instructor_id=request.session.get("instructor_id"),
        drill_id=drill_id,
        note=(note or "").strip() or None,
        status="assigned",
    )
    db.add(a)
    db.commit()
    return JSONResponse({"ok": True})

# --------------- Favorites toggle (fixes POST /favorite/<id> 404) -------------
@app.post("/favorite/{player_id}")
def toggle_favorite(player_id: int, request: Request, db: Session = Depends(get_db)):
    iid = request.session.get("instructor_id")
    if not iid:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # Toggle: delete if exists, else insert
    exists = db.execute(
        text("SELECT 1 FROM favorites WHERE instructor_id = :iid AND player_id = :pid"),
        {"iid": iid, "pid": player_id}
    ).first()

    if exists:
        db.execute(
            text("DELETE FROM favorites WHERE instructor_id = :iid AND player_id = :pid"),
            {"iid": iid, "pid": player_id}
        )
        db.commit()
        return JSONResponse({"ok": True, "favorite": False})
    else:
        db.execute(
            text("INSERT INTO favorites (instructor_id, player_id) VALUES (:iid, :pid)"),
            {"iid": iid, "pid": player_id}
        )
        db.commit()
        return JSONResponse({"ok": True, "favorite": True})

# ------------------------- Health check ---------------------------------------
@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")
