import os
import secrets
from datetime import datetime, date
from typing import Optional, List

from fastapi import FastAPI, Request, Form, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER
from jinja2 import Environment
from fastapi.templating import Jinja2Templates

from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, DateTime, Float,
    ForeignKey, Text, select, func, text
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session as SASession

# --------------------------------------------------------------------------------------
# Config & app
# --------------------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
# If Render variable points to Postgres, you could swap for a PG engine here.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

app = FastAPI(title="Hit4Power")

# sessions for simple auth
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

# static & templates
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Jinja filters (used by your templates)
def _datetimeformat(value, fmt="%b %d, %Y"):
    if not value:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime(fmt)
    # try parsing common formats
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.strftime(fmt)
    except Exception:
        return str(value)

def _dateonly(value):
    if not value:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(value)

templates.env.filters["datetimeformat"] = _datetimeformat
templates.env.filters["dateonly"] = _dateonly

# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    login_code = Column(String(20), unique=True, nullable=True)
    image_path = Column(String(500), nullable=True)
    favorited = Column(Boolean, default=False)  # used by /favorite/{id}
    created_at = Column(DateTime, default=datetime.utcnow)

    metrics = relationship("Metric", back_populates="player", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="player", cascade="all, delete-orphan")
    assignments = relationship("DrillAssignment", back_populates="player", cascade="all, delete-orphan")


class Metric(Base):
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"), index=True, nullable=False)

    # canonical columns used by UI
    recorded_at = Column(DateTime, default=datetime.utcnow, index=True)
    exit_velocity = Column(Float, nullable=True)
    launch_angle = Column(Float, nullable=True)
    spin_rate = Column(Float, nullable=True)

    # extra columns your logs indicated might be referenced elsewhere
    metric = Column(String(100), nullable=True)
    value = Column(Float, nullable=True)
    unit = Column(String(50), nullable=True)
    source = Column(String(100), nullable=True)
    entered_by_instructor_id = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)

    player = relationship("Player", back_populates="metrics")


class Note(Base):
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"), index=True, nullable=False)
    instructor_id = Column(Integer, nullable=True)
    text = Column(Text, nullable=False)
    shared = Column(Boolean, default=False)
    kind = Column(String(50), default="coach")  # added per earlier error
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    player = relationship("Player", back_populates="notes")


class Drill(Base):
    __tablename__ = "drills"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)

    assignments = relationship("DrillAssignment", back_populates="drill", cascade="all, delete-orphan")


class DrillAssignment(Base):
    __tablename__ = "drill_assignments"
    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"), index=True, nullable=False)
    instructor_id = Column(Integer, nullable=True)
    drill_id = Column(Integer, ForeignKey("drills.id"), nullable=False)
    note = Column(Text, nullable=True)
    status = Column(String(50), default="assigned")
    due_date = Column(DateTime, nullable=True)  # added per earlier error
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    player = relationship("Player", back_populates="assignments")
    drill = relationship("Drill", back_populates="assignments")


# --------------------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------------------

def get_db() -> SASession:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _table_has_column(conn, table: str, column: str) -> bool:
    if not DATABASE_URL.startswith("sqlite"):
        # For non-sqlite, you'd query information_schema
        return True
    res = conn.exec_driver_sql(f"PRAGMA table_info({table});").fetchall()
    cols = {row[1] for row in res}
    return column in cols

def ensure_schema():
    """
    Create tables, then ALTER any missing columns we know templates/routes read from.
    Also backfill some values to avoid None-related crashes.
    """
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        def _apply(sql: str, label: str):
            try:
                conn.exec_driver_sql(sql)
                print(f"[ensure_schema] {label}")
            except Exception as _:
                # column might already exist, ignore
                pass

        # players.favorited
        if not _table_has_column(conn, "players", "favorited"):
            _apply("ALTER TABLE players ADD COLUMN favorited INTEGER DEFAULT 0", "players: added favorited")

        # metrics extended columns
        if not _table_has_column(conn, "metrics", "metric"):
            _apply("ALTER TABLE metrics ADD COLUMN metric TEXT", "metrics: added metric")
        if not _table_has_column(conn, "metrics", "value"):
            _apply("ALTER TABLE metrics ADD COLUMN value REAL", "metrics: added value")
        if not _table_has_column(conn, "metrics", "unit"):
            _apply("ALTER TABLE metrics ADD COLUMN unit TEXT", "metrics: added unit")
        if not _table_has_column(conn, "metrics", "source"):
            _apply("ALTER TABLE metrics ADD COLUMN source TEXT", "metrics: added source")
        if not _table_has_column(conn, "metrics", "entered_by_instructor_id"):
            _apply("ALTER TABLE metrics ADD COLUMN entered_by_instructor_id INTEGER", "metrics: added entered_by_instructor_id")
        if not _table_has_column(conn, "metrics", "note"):
            _apply("ALTER TABLE metrics ADD COLUMN note TEXT", "metrics: added note")
        if not _table_has_column(conn, "metrics", "recorded_at"):
            _apply("ALTER TABLE metrics ADD COLUMN recorded_at TIMESTAMP", "metrics: added recorded_at (nullable)")
            _apply("UPDATE metrics SET recorded_at = COALESCE(recorded_at, CURRENT_TIMESTAMP)", "metrics: backfilled recorded_at")

        # notes.kind
        if not _table_has_column(conn, "notes", "kind"):
            _apply("ALTER TABLE notes ADD COLUMN kind TEXT DEFAULT 'coach'", "notes: added kind")

        # drill_assignments.due_date
        if not _table_has_column(conn, "drill_assignments", "due_date"):
            _apply("ALTER TABLE drill_assignments ADD COLUMN due_date TIMESTAMP", "drill_assignments: added due_date")

    # Seed a couple drills if empty
    with SessionLocal() as db:
        if db.scalar(select(func.count(Drill.id))) == 0:
            db.add_all([
                Drill(title="Tee Work — Line Drives", description="Focus on staying through the ball."),
                Drill(title="Front Toss — Gap to Gap", description="Work on timing and barrel control."),
                Drill(title="Short Bat — Inside Pitch", description="Quick hands, keep knob inside.")
            ])
            db.commit()

def _require_instructor(request: Request):
    if request.session.get("role") != "instructor":
        raise HTTPException(status_code=403, detail="Instructor login required")

def _require_player(request: Request):
    if request.session.get("role") != "player":
        raise HTTPException(status_code=403, detail="Player login required")

# --------------------------------------------------------------------------------------
# Health & probe endpoints (for Render/Google)
# --------------------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
def health():
    # trivial liveness
    return {"ok": True}

@app.get("/ready", include_in_schema=False)
def ready():
    # simple readiness: can we start a DB session?
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception as e:
        return JSONResponse({"ready": False, "error": str(e)}, status_code=500)

@app.head("/", include_in_schema=False)
def root_head():
    # Probes often send HEAD /
    return Response(status_code=200)

# --------------------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    ensure_schema()

# --------------------------------------------------------------------------------------
# Basic pages & auth
# --------------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/login/instructor")
def login_instructor(request: Request):
    request.session.clear()
    request.session["role"] = "instructor"
    request.session["instructor_id"] = 1
    return RedirectResponse(url="/instructor", status_code=HTTP_303_SEE_OTHER)

@app.post("/login/player")
def login_player(request: Request, code: Optional[str] = Form(default=None), db: SASession = Depends(get_db)):
    request.session.clear()
    # if a code is provided, try to match; else pick first player
    player = None
    if code:
        player = db.scalar(select(Player).where(Player.login_code == code))
    if not player:
        player = db.scalar(select(Player).order_by(Player.id.asc()))
    if not player:
        # create a sample player so dashboard works
        player = Player(name="Sample Player", login_code="0000")
        db.add(player)
        db.commit()
        db.refresh(player)
    request.session["role"] = "player"
    request.session["player_id"] = player.id
    return RedirectResponse(url="/dashboard", status_code=HTTP_303_SEE_OTHER)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)

# --------------------------------------------------------------------------------------
# Instructor views
# --------------------------------------------------------------------------------------

@app.get("/instructor", response_class=HTMLResponse)
def instructor_home(request: Request, db: SASession = Depends(get_db)):
    _require_instructor(request)
    players = db.execute(select(Player).order_by(Player.favorited.desc(), Player.created_at.desc())).scalars().all()
    # eager counts for templates that call p.metrics|length
    # (SQLAlchemy lazy loads would also work, but let's be explicit)
    for p in players:
        _ = len(p.metrics)  # triggers load
    return templates.TemplateResponse("instructor_dashboard.html", {"request": request, "players": players})

@app.post("/players/create")
def create_player(request: Request, name: str = Form(...), db: SASession = Depends(get_db)):
    _require_instructor(request)
    code = secrets.token_hex(2)  # short-ish login code
    player = Player(name=name.strip(), login_code=code)
    db.add(player)
    db.commit()
    return RedirectResponse(url="/instructor", status_code=HTTP_303_SEE_OTHER)

@app.post("/favorite/{player_id}")
def toggle_favorite(request: Request, player_id: int, db: SASession = Depends(get_db)):
    _require_instructor(request)
    p = db.get(Player, player_id)
    if not p:
        raise HTTPException(status_code=404, detail="Player not found")
    p.favorited = not bool(p.favorited)
    db.commit()
    return {"favorited": bool(p.favorited)}

@app.get("/instructor/player/{player_id}", response_class=HTMLResponse)
def instructor_player_detail(request: Request, player_id: int, db: SASession = Depends(get_db)):
    _require_instructor(request)
    player = db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    metrics = db.execute(
        select(Metric)
        .where(Metric.player_id == player_id)
        .order_by(Metric.recorded_at.desc())
        .limit(50)
    ).scalars().all()

    notes = db.execute(
        select(Note)
        .where(Note.player_id == player_id)
        .order_by(Note.created_at.desc())
        .limit(50)
    ).scalars().all()

    drills = db.execute(select(Drill).order_by(Drill.title.asc())).scalars().all()

    ctx = {
        "request": request,
        "player": player,
        "metrics": metrics,
        "notes": notes,
        "drills": drills,
    }
    return templates.TemplateResponse("instructor_player_detail.html", ctx)

@app.post("/metrics/add")
def add_metrics(
    request: Request,
    player_id: int = Form(...),
    date_str: Optional[str] = Form(default=None, alias="date"),
    exit_velocity: Optional[float] = Form(default=None),
    launch_angle: Optional[float] = Form(default=None),
    spin_rate: Optional[float] = Form(default=None),
    db: SASession = Depends(get_db),
):
    _require_instructor(request)
    player = db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    recorded_at = None
    if date_str:
        try:
            recorded_at = datetime.fromisoformat(date_str)
        except Exception:
            recorded_at = datetime.utcnow()

    m = Metric(
        player_id=player_id,
        recorded_at=recorded_at or datetime.utcnow(),
        exit_velocity=exit_velocity,
        launch_angle=launch_angle,
        spin_rate=spin_rate,
    )
    db.add(m)
    db.commit()
    return RedirectResponse(url=f"/instructor/player/{player_id}", status_code=HTTP_303_SEE_OTHER)

@app.post("/notes/add")
def add_note(
    request: Request,
    player_id: int = Form(...),
    text_value: str = Form(..., alias="text"),
    share_with_player: Optional[str] = Form(default=None),
    text_player: Optional[str] = Form(default=None),  # flag available if you later text via Twilio
    db: SASession = Depends(get_db),
):
    _require_instructor(request)
    player = db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    n = Note(
        player_id=player_id,
        instructor_id=request.session.get("instructor_id"),
        text=text_value.strip(),
        shared=bool(share_with_player),
        kind="coach",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(n)
    db.commit()

    # If you later want to text the player, hook Twilio here when text_player is set.

    return RedirectResponse(url=f"/instructor/player/{player_id}", status_code=HTTP_303_SEE_OTHER)

@app.post("/drills/assign")
def assign_drill(
    request: Request,
    player_id: int = Form(...),
    drill_id: int = Form(...),
    note: Optional[str] = Form(default=None),
    db: SASession = Depends(get_db),
):
    _require_instructor(request)
    player = db.get(Player, player_id)
    drill = db.get(Drill, drill_id)
    if not player or not drill:
        raise HTTPException(status_code=404, detail="Player or Drill not found")

    a = DrillAssignment(
        player_id=player_id,
        instructor_id=request.session.get("instructor_id"),
        drill_id=drill_id,
        note=note.strip() if note else None,
        status="assigned",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return RedirectResponse(url=f"/instructor/player/{player_id}", status_code=HTTP_303_SEE_OTHER)

# --------------------------------------------------------------------------------------
# Player dashboard
# --------------------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: SASession = Depends(get_db)):
    _require_player(request)
    player_id = request.session.get("player_id")
    player = db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # last shared note for player
    last_note = db.execute(
        select(Note)
        .where(Note.player_id == player_id, Note.shared == True)  # noqa: E712
        .order_by(Note.created_at.desc())
        .limit(1)
    ).scalars().first()

    # chart data: last 20 metrics by recorded_at asc (for time-series)
    rows: List[Metric] = db.execute(
        select(Metric)
        .where(Metric.player_id == player_id)
        .order_by(Metric.recorded_at.asc())
        .limit(20)
    ).scalars().all()

    dates = [(_dateonly(m.recorded_at)) for m in rows]
    exitv = [(m.exit_velocity or 0.0) for m in rows]

    # Always provide arrays so Jinja tojson doesn't see "Undefined"
    ctx = {
        "request": request,
        "player": player,
        "last_note": last_note,
        "dates": dates or [],
        "exitv": exitv or [],
    }
    return templates.TemplateResponse("dashboard.html", ctx)
