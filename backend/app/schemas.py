from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from app.models import PageStatus, Role, VersionSource


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    display_name: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreateIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    display_name: str
    role: Role = Role.expert


class UserUpdateIn(BaseModel):
    display_name: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6)


class JobOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    progress: dict[str, Any] = {}
    error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectOut(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    title_sa: str | None
    status: str
    settings: dict[str, Any]
    page_count: int = 0
    pdf_pages: int | None = None
    draft_ready: int = 0
    accepted: int = 0
    source_kind: str = "scan"  # scan | text
    confirm_required: bool = False  # True if >100 pages and pipeline not started yet
    pipeline: JobOut | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectCreateIn(BaseModel):
    slug: str = Field(min_length=2, max_length=128, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    title: str
    title_sa: str | None = None
    extract_from: int = Field(default=1, ge=1)
    extract_to: int | None = Field(default=None, ge=1)


class ProjectSettingsIn(BaseModel):
    settings: dict[str, Any]


class PageOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    page_no: int
    status: PageStatus
    has_scan: bool
    has_html: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class PageDetailOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    page_no: int
    status: PageStatus
    current_html: str | None
    scan_url: str | None
    updated_at: datetime


class PageHtmlIn(BaseModel):
    html: str
    note: str | None = None


class PageReviseIn(BaseModel):
    """Natural-language directive to re-draft the page from its scan."""
    directive: str = Field(min_length=3, max_length=4000)


class PageReviewAgainIn(BaseModel):
    """Optional note; empty → default «пересмотри страницу»."""
    directive: str | None = Field(default=None, max_length=4000)


class PageVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    source: VersionSource
    note: str | None
    created_at: datetime
    created_by: uuid.UUID | None

    model_config = {"from_attributes": True}


class ExtractIn(BaseModel):
    extract_from: int = Field(default=1, ge=1)
    extract_to: int | None = Field(default=None, ge=1)


class LlmUsageTotalsOut(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0


class LlmUsageNetworkOut(BaseModel):
    network: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    est_usd: float | None = None


class LlmUsageModelOut(BaseModel):
    network: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    est_usd: float | None = None


class ProjectUsageOut(BaseModel):
    project_id: str
    totals: LlmUsageTotalsOut
    by_network: list[LlmUsageNetworkOut]
    by_model: list[LlmUsageModelOut]
    est_usd_total: float | None = None


class LlmCatalogOut(BaseModel):
    models: list[dict[str, str]]
    note: str


class LlmRouteOut(BaseModel):
    route: str
    label: str
    hint: str
    options: list[dict[str, Any]]
    primary: dict[str, str]
    fallback_models: dict[str, str]
    updated_at: float | None = None


class LlmRouteIn(BaseModel):
    route: str
