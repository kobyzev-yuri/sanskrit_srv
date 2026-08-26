from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models import PageStatus, Role, VersionSource


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    display_name: str


class LoginIn(BaseModel):
    email: str = Field(min_length=1)  # email or login
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    login: str = ""
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime
    allow_default_llm: bool = True
    use_default_llm: bool = True
    llm_route: str | None = None
    has_openrouter_key: bool = False
    has_proxyapi_key: bool = False

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _from_orm(cls, data: Any):
        if isinstance(data, dict):
            return data
        return {
            "id": data.id,
            "email": data.email,
            "login": getattr(data, "login", None) or data.email,
            "display_name": data.display_name,
            "role": data.role,
            "is_active": data.is_active,
            "created_at": data.created_at,
            "allow_default_llm": bool(getattr(data, "allow_default_llm", True)),
            "use_default_llm": bool(getattr(data, "use_default_llm", True)),
            "llm_route": getattr(data, "llm_route", None) or None,
            "has_openrouter_key": bool((getattr(data, "openrouter_api_key", None) or "").strip()),
            "has_proxyapi_key": bool((getattr(data, "proxyapi_key", None) or "").strip()),
        }


class UserCreateIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    display_name: str
    login: str | None = None
    role: Role = Role.expert
    allow_default_llm: bool = True


class UserUpdateIn(BaseModel):
    display_name: str | None = None
    login: str | None = None
    email: EmailStr | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6)
    allow_default_llm: bool | None = None


class MeUpdateIn(BaseModel):
    email: EmailStr | None = None
    login: str | None = None
    display_name: str | None = None
    current_password: str | None = None
    password: str | None = Field(default=None, min_length=6)


class MeLlmUpdateIn(BaseModel):
    use_default_llm: bool | None = None
    llm_route: str | None = None
    openrouter_api_key: str | None = None
    proxyapi_key: str | None = None


class MeLlmOut(BaseModel):
    allow_default_llm: bool
    use_default_llm: bool
    llm_route: str | None = None
    effective_route: str
    effective_label: str
    key_source: str
    has_openrouter_key: bool
    has_proxyapi_key: bool
    openrouter_hint: str | None = None
    proxyapi_hint: str | None = None
    options: list[dict[str, Any]] = []
    default_route: str
    default_label: str
    default_openrouter_key: bool
    default_proxyapi_key: bool


class JobOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    progress: dict[str, Any] = {}
    error: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_validator("status", mode="before")
    @classmethod
    def _job_status_str(cls, v: Any) -> str:
        if hasattr(v, "value"):
            return str(v.value)
        text = str(v or "")
        return text.split(".")[-1] if text else ""

    @field_validator("progress", mode="before")
    @classmethod
    def _job_progress_dict(cls, v: Any) -> dict[str, Any]:
        if not v:
            return {}
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v if isinstance(v, dict) else {}


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
    task: str = "digitize"  # digitize | translate
    manual_pages: bool = False  # True when auto whole-book pipeline was skipped (>N pages)
    translation: dict[str, Any] | None = None
    source_project_id: str | None = None
    confirm_required: bool = False  # unused; kept for older UI
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


class ProofreadSuggestion(BaseModel):
    id: str
    wrong: str
    right: str
    reason: str = ""
    severity: str = "medium"
    kind: str = ""  # incomplete | join | sanskrit | sense
    target: str = "draft"  # draft | source | both


class ProofreadOut(BaseModel):
    suggestions: list[ProofreadSuggestion]
    model: str = ""
    note: str = ""


class ProofreadApplyIn(BaseModel):
    """Expert-accepted subset of proofread suggestions."""
    accepted: list[ProofreadSuggestion] = Field(default_factory=list)


class DraftSearchHitOut(BaseModel):
    page_id: str
    page_no: int
    count: int
    fields: list[str] = []
    snippets: list[str] = []


class DraftSearchOut(BaseModel):
    query: str
    page_hits: int = 0
    total_matches: int = 0
    hits: list[DraftSearchHitOut] = []
    truncated: bool = False


class PageOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    page_no: int
    status: PageStatus
    has_scan: bool
    has_html: bool
    has_source_html: bool = False
    proof_n: int = 0
    updated_at: datetime

    model_config = {"from_attributes": True}


class PageDetailOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    page_no: int
    status: PageStatus
    current_html: str | None
    source_html: str | None = None
    scan_url: str | None
    proofread: ProofreadOut | None = None
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


class TranslationStyleIn(BaseModel):
    style: str | None = None
    english_comments: str | None = None
    notes: str | None = Field(default=None, max_length=4000)
    agree: bool | None = None


class SpawnTranslationIn(BaseModel):
    slug: str = Field(min_length=2, max_length=128, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    title: str | None = None
    style: str = "interlinear"
    english_comments: str = "replace"
    notes: str = ""


class PageTranslateIn(BaseModel):
    directive: str | None = Field(default=None, max_length=4000)


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


class AdminUsageUserOut(BaseModel):
    user_id: str | None = None
    login: str | None = None
    email: str | None = None
    display_name: str
    key_source: str
    key_hint: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    by_network: list[LlmUsageNetworkOut] = []


class ProjectUsageOut(BaseModel):
    project_id: str
    totals: LlmUsageTotalsOut
    by_network: list[LlmUsageNetworkOut]
    by_model: list[LlmUsageModelOut]
    est_usd_total: float | None = None
    route: str | None = None
    route_label: str | None = None
    route_model: str | None = None
    by_user: list[AdminUsageUserOut] = []


class AdminUsageProjectOut(BaseModel):
    project_id: str
    slug: str
    title: str
    task: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    by_network: list[LlmUsageNetworkOut] = []


class AdminUsageOut(BaseModel):
    projects: list[AdminUsageProjectOut]
    totals: LlmUsageTotalsOut
    by_network: list[LlmUsageNetworkOut] = []
    by_user: list[AdminUsageUserOut] = []


class MeUsageOut(BaseModel):
    totals: LlmUsageTotalsOut
    by_user: list[AdminUsageUserOut] = []


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
    openrouter_key: bool = False
    proxyapi_key: bool = False
    key_source: str = "default"
    use_default: bool = True


class LlmRouteIn(BaseModel):
    route: str
