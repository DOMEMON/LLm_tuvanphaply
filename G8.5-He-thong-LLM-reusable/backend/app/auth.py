import hashlib
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.errors import APIError
from app.models import SessionRecord, User, utcnow


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def account_cookie(user_id: UUID) -> str:
    return f"{get_settings().session_cookie_name}_{user_id.hex}"


def request_token(request: Request) -> str | None:
    account = request.headers.get("X-Account-ID")
    if account:
        try:
            return request.cookies.get(account_cookie(UUID(account)))
        except ValueError:
            return None
    if get_settings().auth_demo_login_enabled:
        return request.cookies.get(get_settings().session_cookie_name)
    return None


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request_token(request)
    if token:
        user = await db.scalar(
            select(User)
            .join(SessionRecord)
            .where(
                SessionRecord.token_hash == token_hash(token),
                SessionRecord.expires_at > utcnow(),
            )
        )
        selected = request.headers.get("X-Account-ID")
        if user and (not selected or user.id == UUID(selected)):
            if user.is_guest and request.headers.get("X-G6-Share-Account-Required") == "1":
                raise APIError(401, "ACCOUNT_REQUIRED", "Vui lòng đăng ký hoặc đăng nhập.")
            return user
    raise APIError(401, "UNAUTHORIZED", "Vui lòng đăng nhập.")
