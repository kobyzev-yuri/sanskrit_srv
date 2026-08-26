"""SQLAlchemy models — SQLite-friendly types for small VPS deploy."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    admin = "admin"
    expert = "expert"
    scholar = "scholar"
    reader = "reader"


class PageStatus(str, enum.Enum):
    pending = "pending"
    extracting = "extracting"
    ocr = "ocr"
    llm_draft = "llm_draft"
    expert_review = "expert_review"
    expert_done = "expert_done"
    scholar_review = "scholar_review"
    published = "published"


class VersionSource(str, enum.Enum):
    ocr = "ocr"
    llm = "llm"
    expert = "expert"
    scholar_assistant = "scholar_assistant"
    scholar = "scholar"
    seed = "seed"


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    login: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.expert)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Admin may grant shared .env / backoffice keys. Expert may still store own keys.
    allow_default_llm: Mapped[bool] = mapped_column(default=True)
    use_default_llm: Mapped[bool] = mapped_column(default=True)
    llm_route: Mapped[str | None] = mapped_column(String(32), nullable=True)
    openrouter_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    proxyapi_key: Mapped[str | None] = mapped_column(Text, nullable=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    title_sa: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    pages: Mapped[list[Page]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("project_id", "page_no"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    scan_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PageStatus] = mapped_column(Enum(PageStatus), default=PageStatus.pending)
    current_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Verified Sanskrit HTML (left pane in translate task). Digitize projects leave this null.
    source_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_expert_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    project: Mapped[Project] = relationship(back_populates="pages")
    versions: Mapped[list[PageVersion]] = relationship(cascade="all, delete-orphan")


class PageVersion(Base):
    __tablename__ = "page_versions"
    __table_args__ = (UniqueConstraint("page_id", "version"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("pages.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    html: Mapped[str] = mapped_column(Text)
    source: Mapped[VersionSource] = mapped_column(Enum(VersionSource))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssistantTurn(Base):
    __tablename__ = "assistant_turns"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("pages.id"), index=True)
    scholar_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    directive: Mapped[str] = mapped_column(Text)
    proposed_html: Mapped[str] = mapped_column(Text)
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class Job(Base):
    """Background work: project pipeline (extract + LLM draft for all pages)."""

    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64), default="pipeline_project")
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), index=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("pages.id"), nullable=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LlmUsageEvent(Base):
    """One successful ProxyAPI call — for per-project billing by network/model."""

    __tablename__ = "llm_usage_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), index=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("pages.id"), nullable=True, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    network: Mapped[str] = mapped_column(String(32), index=True)  # gemini | openai
    model: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(64), default="auto_draft")
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    key_source: Mapped[str] = mapped_column(String(16), default="default")  # default | personal
    key_hint: Mapped[str | None] = mapped_column(String(8), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_raw: Mapped[dict] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
