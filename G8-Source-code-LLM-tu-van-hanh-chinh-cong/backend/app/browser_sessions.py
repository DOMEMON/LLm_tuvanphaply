"""Renew an authenticated browser session without rotating its bearer token.

Installed only in the G8/G8.5 web shell. Never accepts an identity or token in
the body, revives an expired session, or modifies conversation ownership.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import account_cookie, current_user, request_token, token_hash
from app.config import get_settings
from app.db import get_db
from app.errors import APIError
from app.models import SessionRecord, User, utcnow
from app.schemas import UserOutput

router = APIRouter(prefix='/api/v1/auth', tags=['browser-session'])


@router.post('/browser-session', response_model=UserOutput)
async def renew_browser_session(
    request: Request, response: Response,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    token = request_token(request)
    if not token:
        raise APIError(401, 'UNAUTHORIZED', 'Phiên trình duyệt đã hết hạn.')
    settings = get_settings()
    now = utcnow()
    result = await db.execute(update(SessionRecord).where(
        SessionRecord.user_id == user.id,
        SessionRecord.token_hash == token_hash(token),
        SessionRecord.expires_at > now,
    ).values(expires_at=now + timedelta(seconds=settings.session_ttl_seconds)))
    if result.rowcount != 1:
        await db.rollback()
        raise APIError(401, 'UNAUTHORIZED', 'Phiên trình duyệt đã hết hạn.')
    await db.commit()
    # Same bearer: another tab's in-flight authenticated request stays valid.
    response.set_cookie(account_cookie(user.id), token,
        max_age=settings.session_ttl_seconds, httponly=True,
        secure=settings.session_cookie_secure, samesite='lax', path='/')
    return user
