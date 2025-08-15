# app/routes.py
import os
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette import status

from sqlalchemy import func, select, and_
from sqlalchemy.orm import Session

from .database import SessionLocal, engine, Base
from .models import (
    Player, Instructor, Metric, Note, Drill, DrillAssignment, InstructorFavorite
)
from .utils import (
    normalize_code, hash_code, set_flash, pop_flash, age_bucket
)

# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------
app = FastAPI()

# Sessions (required for flash + auth)
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-insecure-session-secret")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False)

# Static files & templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Create tables if they don't exist yet (ok for demo; use Alembic in prod)
Base.metadata.create_all(bind=engine)


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
    """
    Flexible login lookup:
      1) Try normalized plaintext match (if you store normalized codes)
      2) Try hashed match (if you store HMAC of normalized)
    Works for Player and Instructor since both have .login_code.
    """
    norm = normalize_code(raw_code)
    # a) plaintext normalized
    obj = db.execute(
        select(model).where(model.login_code == norm)
    ).scalar_one_or_none()
    if obj:
        return obj
    # b) hashed
    hashed = hash_code(raw_code)
    obj = db.execute(
        select(model).where(model.login_code == hashed)
    ).scalar_one_or_none()
    return obj


def _latest_metrics_query(player_id: int):
    """
    Returns a selectable that yields the latest row per (player_id, metric).
    """
    subq = (
        select(
            Metric.metric.label("metric"),
            func.max(Metric.recorded_at).label("latest")
        )
        .where(Metric.player_id == player_id)
        .group_by(Metric.metric)
        .subquery()
    )

    stmt = (
        select(Metric)
        .where(Metric.player_id == player_id)
        .join(
            subq,
            and_(
                Metric.metric == subq.c.metric,
                Metric.recorded_at == subq.c.latest,
            )
        )
        .order_by(Metric.metric.asc())
    )
    return stmt


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    # Always pass {"request": request} to templates; flash is read-only in templates
    ctx = {"request": request, "flash": pop_flash(request)}
    return templates.TemplateResponse("index.html", ctx)


@app.post("/login/player")
def login_player(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    player = _login_lookup(db, Player, code)
    if not player:
        set_flash(request, "Invalid code. Please try again.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    # Auth OK
    request.session["player_id"] = player.id
    # Clear any instructor session
    request.session.pop("instructor_id", None)
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/login/instructor")
def login_instructor(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    coach = _login_lookup(db, Instructor, code)
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

    # Most recent shared note
    last_note = db.execute(
        select(Note)
        .where(Note.player_id == pid, Note.shared == True)  # noqa: E712
        .order_by(Note.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    # Active drill assignments
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
    iid = request.session.get("instructor_id")
    if not iid:
        set_flash(request, "Please log in as an instructor.")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    # Simple roster ordered by most recently updated metric or player update
    last_metric_subq = (
        select(
            Metric.player_id.label("pid"),
            func.max(Metric.recorded_at).label("last_metric_at")
        )
        .group_by(Metric.player_id)
        .subquery()
    )

    rows = db.execute(
        select(Player, last_metric_subq.c.last_metric_at)
        .outerjoin(last_metric_subq, Player.id == last_metric_subq.c.pid)
        .order_by(func.coalesce(last_metric_subq.c.last_metric_at, Player.updated_at).desc())
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

    when = datetime.fromisoformat(recorded_at) if recorded_at else datetime.utcnow()

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
