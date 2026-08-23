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
            print(f"{u.email}\t{u.role.value}\t{flag}\t{u.display_name}")


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

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
