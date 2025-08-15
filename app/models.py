from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Float, Boolean, Text,
    UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from .database import Base


# ---------- Mixin ----------
class TimestampMixin:
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------- Core entities ----------
class Instructor(Base, TimestampMixin):
    __tablename__ = "instructors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, default="Coach")

    # Store the (optionally hashed) code; ALWAYS normalize on write/read.
    login_code = Column(String(128), unique=True, index=True, nullable=False)

    favorites = relationship("InstructorFavorite", back_populates="instructor", cascade="all, delete-orphan")


class Player(Base, TimestampMixin):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    age = Column(Integer, nullable=False, default=12)

    # Store the (optionally hashed) code; ALWAYS normalize on write/read.
    login_code = Column(String(128), unique=True, index=True, nullable=False)

    phone = Column(String(32), nullable=True)
    image_path = Column(String(255), nullable=True)

    metrics = relationship("Metric", back_populates="player", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="player", cascade="all, delete-orphan")
    drills = relationship("DrillAssignment", back_populates="player", cascade="all, delete-orphan")


# ---------- Time-series metrics ----------
class Metric(Base):
    """
    Generic time-series metric. Examples of metric names:
    'exit_velocity', 'bat_speed', 'launch_angle', 'attack_angle', etc.
    """
    __tablename__ = "metrics"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)

    # Name of the metric (snake_case). Keep short & consistent.
    metric = Column(String(64), nullable=False, index=True)

    # Numeric value + optional unit. (Keep values numeric for charts.)
    value = Column(Float, nullable=False)
    unit = Column(String(24), nullable=True)  # e.g., 'mph', 'deg'

    # Who/when this entry was captured
    recorded_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    source = Column(String(64), nullable=True)  # 'instructor', 'sensor', 'import'
    entered_by_instructor_id = Column(Integer, ForeignKey("instructors.id"), nullable=True)

    note = Column(Text, nullable=True)

    player = relationship("Player", back_populates="metrics")

    __table_args__ = (
        # Fast lookups for trend charts and "latest value"
        Index("ix_metrics_player_metric_time", "player_id", "metric", "recorded_at"),
    )


# ---------- Notes & lessons ----------
class Note(Base, TimestampMixin):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    instructor_id = Column(Integer, ForeignKey("instructors.id"), nullable=True)

    text = Column(Text, nullable=False)

    # If True, show to player on dashboard; if False, instructor-only.
    shared = Column(Boolean, default=True, nullable=False)

    # Optional categorization
    kind = Column(String(32), nullable=True)  # e.g., 'lesson', 'observation'

    player = relationship("Player", back_populates="notes")


# ---------- Drills ----------
class Drill(Base, TimestampMixin):
    __tablename__ = "drills"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    video_url = Column(String(500), nullable=True)  # file path or external URL


class DrillAssignment(Base, TimestampMixin):
    __tablename__ = "drill_assignments"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    instructor_id = Column(Integer, ForeignKey("instructors.id"), nullable=True, index=True)
    drill_id = Column(Integer, ForeignKey("drills.id"), nullable=False)

    note = Column(Text, nullable=True)
    status = Column(String(24), default="assigned", nullable=False)  # 'assigned' | 'completed' | 'archived'
    due_date = Column(DateTime, nullable=True)

    player = relationship("Player", back_populates="drills")
    drill = relationship("Drill")

    __table_args__ = (
        Index("ix_drill_assign_player_status", "player_id", "status"),
    )


# ---------- Favorites ----------
class InstructorFavorite(Base):
    __tablename__ = "instructor_favorites"

    id = Column(Integer, primary_key=True, index=True)
    instructor_id = Column(Integer, ForeignKey("instructors.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)

    instructor = relationship("Instructor", back_populates="favorites")

    __table_args__ = (
        UniqueConstraint("instructor_id", "player_id", name="uq_instructor_player_fav"),
    )


# ---------- Optional: reference ranges for age buckets ----------
class ReferenceRange(Base):
    """
    Store age-bucket reference values so you can compute deltas/percentiles.
    Example rows:
      ('10-12', 'exit_velocity', 52.0, 'mph')
    """
    __tablename__ = "reference_ranges"

    id = Column(Integer, primary_key=True, index=True)
    age_bucket = Column(String(16), index=True, nullable=False)  # e.g., '10-12'
    metric = Column(String(64), index=True, nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String(24), nullable=True)

    __table_args__ = (
        UniqueConstraint("age_bucket", "metric", name="uq_age_metric_ref"),
    )
