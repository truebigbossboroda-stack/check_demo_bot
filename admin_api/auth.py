from fastapi import Header, HTTPException, status
from .config import get_settings

settings = get_settings()

def require_bearer(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.admin_bearer_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return True