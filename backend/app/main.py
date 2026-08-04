"""Sanskrit SRV — FastAPI app."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import get_engine
from app.models import Base
from app.routers import admin, auth, pages, projects, system
from app.services.storage import ensure_dirs

settings = get_settings()
ensure_dirs()
# Auto-create tables on boot (MVP; Alembic later)
if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.replace("sqlite:///", "", 1)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
Base.metadata.create_all(bind=get_engine())

app = FastAPI(
    title="Sanskrit SRV",
    description="OCR/LLM draft + expert HTML editing for Sanskrit scans",
    version="0.1.0",
)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(pages.router, prefix="/api/v1")


@app.get("/health")
def health():
    return {"status": "ok", "service": "sanskrit_srv", "version": "0.1.0"}


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.is_dir():
    assets = FRONTEND_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")
