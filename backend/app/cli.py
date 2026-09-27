"""Server CLI for bootstrap / ops. Example:
  python -m app.cli user-create --email admin@local --password '...' --role admin --name Admin
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.db import ensure_schema, get_engine, get_session_factory
from app.models import Base, Role, User
from app.services.storage import ensure_dirs


def init_db() -> None:
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    ensure_dirs()
    ensure_schema()
    print(f"OK: database tables ready ({settings.database_url})")


def user_create(email: str, password: str, role: str, name: str) -> None:
    init_db()
    email = email.lower()
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email)):
            print(f"ERROR: user {email} already exists", file=sys.stderr)
            sys.exit(1)
        user = User(
            email=email,
            login=email,
            password_hash=hash_password(password),
            display_name=name,
            role=Role(role),
        )
        db.add(user)
        db.commit()
        print(f"OK: created {email} role={role} id={user.id}")


def user_list() -> None:
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        users = db.scalars(select(User).order_by(User.created_at)).all()
        for u in users:
            flag = "active" if u.is_active else "off"
            print(f"{u.email}\t{getattr(u, 'login', '')}\t{u.role.value}\t{flag}\t{u.display_name}")


def user_reset_password(email: str, password: str) -> None:
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            print("ERROR: not found", file=sys.stderr)
            sys.exit(1)
        user.password_hash = hash_password(password)
        db.commit()
        print(f"OK: password reset for {email}")


def merge_iast_into_ru(
    translate_slug: str,
    iast_slug: str,
    *,
    dry_run: bool = False,
    set_style: bool = True,
) -> None:
    """Copy IAST lines from a transliteration project into a Russian translation draft."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import Page, PageVersion, Project, VersionSource
    from app.services.merge_iast import merge_iast_into_translation
    from app.services.translation_style import (
        STYLE_INTERLINEAR_IAST,
        persist_translation_cfg,
        translation_cfg,
    )

    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        ru = db.scalar(select(Project).where(Project.slug == translate_slug))
        ia = db.scalar(select(Project).where(Project.slug == iast_slug))
        if ru is None or ia is None:
            print(f"ERROR: need both projects ({translate_slug!r}, {iast_slug!r})", file=sys.stderr)
            sys.exit(1)
        ru_pages = {
            p.page_no: p
            for p in db.scalars(select(Page).where(Page.project_id == ru.id)).all()
        }
        ia_pages = {
            p.page_no: p
            for p in db.scalars(select(Page).where(Page.project_id == ia.id)).all()
        }
        tot_ins = tot_skip = tot_un = tot_chg = 0
        for no in sorted(ru_pages):
            pr, pi = ru_pages[no], ia_pages.get(no)
            if pi is None or not (pi.current_html or "").strip():
                continue
            if not (pr.current_html or "").strip():
                continue
            new_html, stats = merge_iast_into_translation(pr.current_html, pi.current_html)
            tot_ins += stats["inserted"]
            tot_skip += stats["skipped_existing"]
            tot_un += stats["unmatched_sa"]
            if new_html.strip() == (pr.current_html or "").strip():
                continue
            tot_chg += 1
            if dry_run:
                print(
                    f"page {no}: +{stats['inserted']} iast "
                    f"(skip {stats['skipped_existing']}, unmatched {stats['unmatched_sa']})"
                )
                continue
            pr.current_html = new_html
            next_ver = (
                db.scalar(
                    select(PageVersion.version)
                    .where(PageVersion.page_id == pr.id)
                    .order_by(PageVersion.version.desc())
                )
                or 0
            ) + 1
            db.add(
                PageVersion(
                    page_id=pr.id,
                    version=next_ver,
                    html=new_html,
                    source=VersionSource.expert,
                    note=f"merge iast from {iast_slug} | +{stats['inserted']}",
                )
            )
        if set_style and not dry_run:
            cfg = translation_cfg(ru)
            cfg["style"] = STYLE_INTERLINEAR_IAST
            persist_translation_cfg(ru, cfg)
            flag_modified(ru, "settings")
        if not dry_run:
            db.commit()
        mode = "dry-run" if dry_run else "wrote"
        print(
            f"OK ({mode}): {translate_slug} ← {iast_slug}: "
            f"pages_changed={tot_chg} inserted={tot_ins} "
            f"skipped_existing={tot_skip} unmatched_sa={tot_un}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sanskrit-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="Create tables and storage dirs")
    p_init.set_defaults(func=lambda _: init_db())

    p_uc = sub.add_parser("user-create", help="Create user (bootstrap admin)")
    p_uc.add_argument("--email", required=True)
    p_uc.add_argument("--password", required=True)
    p_uc.add_argument("--role", default="admin", choices=[r.value for r in Role])
    p_uc.add_argument("--name", default="Admin")
    p_uc.set_defaults(func=lambda a: user_create(a.email, a.password, a.role, a.name))

    p_ul = sub.add_parser("user-list")
    p_ul.set_defaults(func=lambda _: user_list())

    p_ur = sub.add_parser("user-reset-password")
    p_ur.add_argument("--email", required=True)
    p_ur.add_argument("--password", required=True)
    p_ur.set_defaults(func=lambda a: user_reset_password(a.email, a.password))

    p_mi = sub.add_parser(
        "merge-iast",
        help="Insert IAST lines from a transliteration project into a Russian translation",
    )
    p_mi.add_argument("--translate", required=True, help="Slug of translate project (e.g. vijnana-ru)")
    p_mi.add_argument("--iast", required=True, help="Slug of IAST project (e.g. vijnana-iast)")
    p_mi.add_argument("--dry-run", action="store_true")
    p_mi.add_argument(
        "--keep-style",
        action="store_true",
        help="Do not switch translation style to interlinear_iast",
    )
    p_mi.set_defaults(
        func=lambda a: merge_iast_into_ru(
            a.translate, a.iast, dry_run=a.dry_run, set_style=not a.keep_style
        )
    )

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
