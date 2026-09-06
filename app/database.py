import json
from datetime import datetime
from typing import Generator
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    Float,
    Text,
    DateTime,
    ForeignKey,
    event,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, backref, Session
from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
)

# Enable WAL mode for high performance concurrent SQLite operations
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()
    except Exception:
        pass

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Recording(Base):
    __tablename__ = "recordings"

    id = Column(String(36), primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    filename = Column(String(255), nullable=False)
    raw_file_path = Column(String(512), nullable=False)
    processed_file_path = Column(String(512), nullable=True)
    duration_seconds = Column(Float, default=0.0)
    file_size = Column(Integer, default=0)
    source_type = Column(String(50), default="upload")  # "upload", "wechat", "phone", "meeting"
    primary_project_id = Column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(50), default="pending")      # "pending", "processing_audio", "asr_running", "cleaning", "analyzing", "completed", "failed"
    progress = Column(Integer, default=0)              # 0 - 100
    status_message = Column(String(255), default="等待处理")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationships
    raw_segments = relationship("AsrSegment", back_populates="recording", cascade="all, delete-orphan", order_by="AsrSegment.seq_order")
    polished_segments = relationship("PolishedSegment", back_populates="recording", cascade="all, delete-orphan", order_by="PolishedSegment.seq_order")
    insight = relationship("MeetingInsight", back_populates="recording", uselist=False, cascade="all, delete-orphan")
    primary_project = relationship("Project", foreign_keys=[primary_project_id])
    project_links = relationship("RecordingProjectLink", back_populates="recording", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    status = Column(String(50), default="active")  # "active" (进行中), "on_hold" (搁置中), "reactivated" (重启中), "completed" (已完结), "terminated" (已终止)
    parent_id = Column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    color = Column(String(50), default="indigo")  # emerald, blue, amber, purple, rose, cyan
    ai_context = Column(Text, default="")  # custom context/glossary/background for LLM
    current_summary = Column(Text, default="")  # live executive summary across all meetings
    pause_reason = Column(Text, nullable=True)  # reason if on_hold
    revisit_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationships
    timelines = relationship("ProjectTimeline", back_populates="project", cascade="all, delete-orphan", order_by="ProjectTimeline.created_at.desc()")
    recording_links = relationship("RecordingProjectLink", back_populates="project", cascade="all, delete-orphan")
    sub_projects = relationship("Project", backref=backref("parent", remote_side=[id]))


class ProjectTimeline(Base):
    __tablename__ = "project_timelines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="SET NULL"), nullable=True)
    event_title = Column(String(255), nullable=False)
    event_detail = Column(Text, nullable=False)
    event_type = Column(String(50), default="update")  # "update", "decision", "risk", "status_change", "milestone"
    audio_timestamp_ms = Column(Integer, default=0)
    source_title = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="timelines")
    recording = relationship("Recording")


class RecordingProjectLink(Base):
    __tablename__ = "recording_project_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    relevance_notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)

    recording = relationship("Recording", back_populates="project_links")
    project = relationship("Project", back_populates="recording_links")



class AsrSegment(Base):
    __tablename__ = "asr_raw_segments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    speaker_id = Column(String(50), default="Speaker 1")
    start_ms = Column(Integer, nullable=False)  # start time in milliseconds
    end_ms = Column(Integer, nullable=False)    # end time in milliseconds
    raw_text = Column(Text, nullable=False)     # Verbatim text including fillers
    seq_order = Column(Integer, nullable=False)

    recording = relationship("Recording", back_populates="raw_segments")


class PolishedSegment(Base):
    __tablename__ = "polished_segments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    speaker_id = Column(String(50), default="Speaker 1")
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    polished_text = Column(Text, nullable=False)
    seq_order = Column(Integer, nullable=False)

    recording = relationship("Recording", back_populates="polished_segments")


class MeetingInsight(Base):
    __tablename__ = "meeting_insights"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recording_id = Column(String(36), ForeignKey("recordings.id", ondelete="CASCADE"), unique=True, index=True)
    executive_summary = Column(Text, default="")
    topics_json = Column(Text, default="[]")        # list of {title, discussion, key_points}
    decisions_json = Column(Text, default="[]")     # list of string decisions
    action_items_json = Column(Text, default="[]")  # list of {task, owner, due_date, status}
    risks_json = Column(Text, default="[]")         # list of {risk, suggestion}
    project_updates_json = Column(Text, default="[]") # list of project timeline patches
    new_projects_json = Column(Text, default="[]")    # list of proposed new project candidates
    full_report_md = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)

    recording = relationship("Recording", back_populates="insight")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    from app.config import DB_DIR
    try:
        DB_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
