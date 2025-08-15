# app/main.py
import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette import status

from sqlalchemy import func, select, and_, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy import inspect

def _has_column(table: str, col: str) -> bool:
    try:
        cols = {c["name"] for c in inspect(engine).get_columns(table)}
        return col in cols
    except Exception as e:
        print(f"[has_column] cannot inspect {table}: {e}")
        return False


from .database import SessionLocal, engine, Base
from .models import Player, Instructor, Metric, Note, DrillAssignment
from .utils import normalize_code, hash_code, set_flash, pop_flash, age_bucket

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

# Create missing **tables** (not columns)
Base.metadata.create_all(bind=engine)

# --- Ensure legacy DB has the columns your models expect ----------------------
def _ensure_schema():
    insp = inspect(engine)

    def cols(table: str) -> set[str]:
        try:
            return {c["name"] for c in insp.get_columns(table)}
        except Exception as e:
            print(f"[ensure_schema] cannot inspect {table}: {e}")
            return set()

    instr_cols  = cols("instructors")
    player_cols = cols("players")
    metric_cols = cols("metrics")

    dialect = engine.dialect.name

    def _add(table: str, ddl: str):
        # PG supports IF NOT EXISTS; SQLite doesn’t (we checked columns above)
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

    # Players
    if "created_at" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN created_at TEXT")
    if "updated_at" not in player_cols:
        _add("players", "ALTER TABLE players ADD COLUMN updated_at TEXT")

    # Metrics — add and backfill recorded_at (the cause of your current 500)
    if "recorded_at" not in metric_cols:
        _add("metrics", "ALTER TABLE metrics ADD COLUMN recorded_at TEXT")
        with engine.begin() as conn:
            if "created_at" in metric_cols:
                conn.execute(text(
                    "UPDATE metrics SET recorded_at = COALESCE(recorded_at, created_at, CURRENT_TIMESTAMP)"
                ))
            else:
                conn.execute(text(
                    "UPDATE metrics SET recorded_at = COALESCE(recorded_at, CURRENT_TIMESTAMP)"
                ))
        print("[ensure_schema] metrics: backfilled recorded_at")

    # Safety backfill for timestamps (idempotent)
    with engine.begin() as conn:
        conn.execute(text("UPDATE instructors SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE instructors SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE players SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
        conn.execute(text("UPDATE players SET updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"))


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
    subq = (
        select(Metric.metric.label("metric"), func.max(Metric.recorded_at).label("latest"))
        .where(Metric.player_id == player_id)
        .group_by(Metric.metric)
        .subquery()
    )
    return (
        select(Metric)
        .where(Metric.player_id == player_id)
        .join(subq, and_(Metric.metric == subq.c.metric, Metric.recorded_at == subq.c.latest))
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
    # First try DB
    coach = _login_lookup(db, Instructor, code)

    # Fallback to env var (INSTRUCTOR_DEFAULT_CODE)
    if not coach:
        env_code = os.getenv("INSTRUCTOR_DEFAULT_CODE", "")
        if env_code and normalize_code(code) == normalize_code(env_code):
            # find-or-create a default instructor; persist the normalized code so future logins hit DB path
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
        select(Note).where(Note.player_id == pid, Note.shared == True).order_by(Note.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    drills = db.execute(
        select(DrillAssignment)
        .where(DrillAssignment.player_id == pid, DrillAssignment.status != "archived")
        .order_by(DrillAssignment.created_at.desc())
    ).scalars().all()

    ctx = {
        "request": request,
        "flash": pop_flash(request),
        "player": player,
        "age_bucket": age_bucket(player.age),
        "latest_metrics": latest_metrics,
        "last_note": last_note,
        "drill_assignments": drills,
    }
    return templates.TemplateResponse("dashboard.html", ctx)

# ------------------------- Instructor views -----------------------------------
@app.get("/instructor")
def instructor_home(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("instructor_id"):
        set_flash(request, "Please log in as an instructor.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    # Decide which timestamp column to use for "last metric"
    use_recorded = _has_column("metrics", "recorded_at")
    time_col = Metric.recorded_at if use_recorded else Metric.created_at

    last_metric_subq = (
        select(
            Metric.player_id.label("pid"),
            func.max(time_col).label("last_metric_at"),
        )
        .group_by(Metric.player_id)
        .subquery()
    )

    rows = db.execute(
        select(Player, last_metric_subq.c.last_metric_at)
        .outerjoin(last_metric_subq, Player.id == last_metric_subq.c.pid)
        # SQLite has no NULLS LAST; coalesce to an old date so NULLs sort last
        .order_by(func.coalesce(last_metric_subq.c.last_metric_at, text("'1970-01-01 00:00:00'")).desc(), Player.id.desc())
    ).all()


    players = [{"player": r[0], "last_update": r[1]} for r in rows]
    ctx = {"request": request, "flash": pop_flash(request), "players": players}
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

# ------------------------- Health check ---------------------------------------
@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")
