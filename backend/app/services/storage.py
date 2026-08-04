from pathlib import Path
import shutil
import uuid

from app.config import get_settings


def ensure_dirs() -> Path:
    root = get_settings().storage_root
    root.mkdir(parents=True, exist_ok=True)
    (root / "projects").mkdir(exist_ok=True)
    return root


def project_dir(project_id: uuid.UUID) -> Path:
    path = ensure_dirs() / "projects" / str(project_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "pages").mkdir(exist_ok=True)
    return path


def save_upload_pdf(project_id: uuid.UUID, filename: str, data: bytes) -> Path:
    dest = project_dir(project_id) / "source.pdf"
    dest.write_bytes(data)
    # keep original name hint
    (project_dir(project_id) / "source_name.txt").write_text(filename, encoding="utf-8")
    return dest


def page_png_path(project_id: uuid.UUID, page_no: int) -> Path:
    return project_dir(project_id) / "pages" / f"{page_no:04d}.png"


def remove_project_files(project_id: uuid.UUID) -> None:
    path = ensure_dirs() / "projects" / str(project_id)
    if path.exists():
        shutil.rmtree(path)
