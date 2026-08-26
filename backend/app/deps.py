import uuid
from collections.abc import Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth import TokenError, safe_decode
from app.db import get_db
from app.models import Role, User
from app.services.llm_route import bind_llm_user, reset_llm_user

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Generator[User, None, None]:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = safe_decode(creds.credentials)
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User inactive or missing")
    token = bind_llm_user(user)
    try:
        yield user
    finally:
        reset_llm_user(token)


def require_roles(*roles: Role):
    def _inner(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return _inner


RequireAdmin = Depends(require_roles(Role.admin))
RequireExpert = Depends(require_roles(Role.admin, Role.expert))
RequireScholar = Depends(require_roles(Role.admin, Role.scholar))
