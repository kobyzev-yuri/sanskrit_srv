"""Background worker: process queued pipeline jobs one at a time.

  PYTHONPATH=backend python -m app.worker
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.db import get_engine, get_session_factory
from app.models import Base, Job, JobStatus
from app.services.pipeline import run_pipeline_job
from app.services.storage import ensure_dirs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sanskrit.worker")


def reclaim_stale_running_jobs(db) -> int:
    """After restart, jobs left in 'running' would never finish — re-queue them."""
    stale = list(db.scalars(select(Job).where(Job.status == JobStatus.running)).all())
    for job in stale:
        job.status = JobStatus.queued
        progress = dict(job.progress or {})
        progress["reclaimed"] = True
        job.progress = progress
        log.warning("requeued stale running job %s (was at page %s)", job.id, progress.get("current_page"))
    if stale:
        db.commit()
    return len(stale)


def main() -> None:
    ensure_dirs()
    Base.metadata.create_all(bind=get_engine())
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        n = reclaim_stale_running_jobs(db)
        if n:
            log.info("reclaimed %s stale running job(s)", n)
    log.info("worker started")
    while True:
        with SessionLocal() as db:
            job = db.scalar(
                select(Job)
                .where(Job.status == JobStatus.queued)
                .order_by(Job.created_at)
                .limit(1)
            )
            if job is None:
                time.sleep(3)
                continue
            log.info("picked job %s kind=%s project=%s", job.id, job.kind, job.project_id)
            try:
                if job.kind == "pipeline_project":
                    run_pipeline_job(db, job)
                else:
                    job.status = JobStatus.failed
                    job.error = f"unknown kind {job.kind}"
                    db.commit()
                log.info("job %s -> %s", job.id, job.status)
            except Exception:
                log.exception("job %s crashed", job.id)
                # Leave job runnable again after crash/restart.
                job = db.get(Job, job.id)
                if job is not None and job.status == JobStatus.running:
                    job.status = JobStatus.queued
                    db.commit()
                time.sleep(2)


if __name__ == "__main__":
    main()
