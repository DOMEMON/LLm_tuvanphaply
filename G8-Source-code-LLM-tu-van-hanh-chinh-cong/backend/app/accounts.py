"""Account-scoped HttpOnly sessions. A tab stores an account ID, never its secret."""

import asyncio
import secrets
from datetime import timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import account_cookie, current_user, token_hash
from app.config import get_settings
from app.db import get_db
from app.errors import APIError
from app.models import AuthLimit, SessionRecord, User, utcnow
from app.schemas import UserOutput

router = APIRouter(prefix="/api/v1/auth", tags=["accounts"])
hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
dummy_hash = hasher.hash(secrets.token_urlsafe(32))


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        return value.lower()


class Registration(Credentials):
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)
    password_confirmation: str = Field(min_length=8, max_length=128)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Display name is required")
        return value

    @model_validator(mode="after")
    def matching_passwords(self):
        if self.password != self.password_confirmation:
            raise ValueError("Passwords must match")
        return self


class PasswordChange(Credentials):
    password: str = Field(min_length=8, max_length=128)
    password_confirmation: str = Field(min_length=8, max_length=128)
    current_password: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def matching_passwords(self):
        if self.password != self.password_confirmation:
            raise ValueError("Passwords must match")
        return self


async def throttle(db: AsyncSession, request: Request, action: str, identity="", limit=30):
    # Shared DB counters work across workers; never trust a client-supplied forwarding IP.
    key = token_hash(f"{request.client.host if request.client else 'unknown'}|{action}|{identity}")
    now = utcnow()
    await db.execute(delete(AuthLimit).where(AuthLimit.expires_at < now))
    statement = insert(AuthLimit).values(
        key=key, attempts=1, expires_at=now + timedelta(minutes=10)
    )
    count = await db.scalar(
        statement.on_conflict_do_update(
            index_elements=[AuthLimit.key],
            set_={"attempts": AuthLimit.attempts + 1},
        ).returning(AuthLimit.attempts)
    )
    await db.commit()
    if count > limit:
        raise APIError(
            429, "AUTH_RATE_LIMIT", "Bạn đã thử nhiều lần. Vui lòng thử lại sau 10 phút."
        )


def verify_password(encoded: str | None, password: str) -> bool:
    try:
        valid = hasher.verify(encoded or dummy_hash, password)
        return bool(encoded and valid)
    except (VerificationError, InvalidHashError):
        return False


async def optional_guest(request: Request, db: AsyncSession) -> User | None:
    try:
        user = await current_user(request, db)
        return user if user.is_guest else None
    except APIError:
        return None


async def issue_session(db: AsyncSession, request: Request, response: Response, user: User):
    await db.flush()
    name = account_cookie(user.id)
    settings = get_settings()
    old = request.cookies.get(name)
    await db.execute(delete(SessionRecord).where(SessionRecord.expires_at < utcnow()))
    if old:
        await db.execute(delete(SessionRecord).where(SessionRecord.token_hash == token_hash(old)))
    # Keep browser cookie headers bounded. Never sign out another account to make room.
    prefix = settings.session_cookie_name + "_"
    cookies = [
        value for key, value in request.cookies.items() if key.startswith(prefix) and key != name
    ]
    if cookies:
        count = len(
            (
                await db.scalars(
                    select(SessionRecord.id).where(
                        SessionRecord.token_hash.in_([token_hash(value) for value in cookies]),
                        SessionRecord.expires_at > utcnow(),
                    )
                )
            ).all()
        )
        if count >= 8:
            await db.rollback()
            raise APIError(
                409, "ACCOUNT_LIMIT", "Hãy đăng xuất một tài khoản trước khi thêm tài khoản mới."
            )
    token = secrets.token_urlsafe(32)
    db.add(
        SessionRecord(
            user_id=user.id,
            token_hash=token_hash(token),
            expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    await db.commit()
    response.set_cookie(
        name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return user


@router.get("/config")
async def auth_config(request: Request):
    return {
        "guest_enabled": request.headers.get("X-G6-Share-Account-Required") != "1",
        "password_enabled": True,
    }


@router.get("/accounts", response_model=list[UserOutput])
async def accounts(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    prefix = get_settings().session_cookie_name + "_"
    found = []
    for name, token in list(request.cookies.items())[:100]:
        if not name.startswith(prefix):
            continue
        suffix = name.removeprefix(prefix)
        try:
            account_id = UUID(hex=suffix)
        except ValueError:
            continue
        user = await db.scalar(
            select(User)
            .join(SessionRecord)
            .where(
                User.id == account_id,
                SessionRecord.token_hash == token_hash(token),
                SessionRecord.expires_at > utcnow(),
            )
        )
        if user:
            found.append(user)
        else:
            response.delete_cookie(name, path="/")
    return found


@router.post("/register", response_model=UserOutput, status_code=201)
async def register(
    body: Registration, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    await throttle(db, request, "register", limit=20)
    guest = await optional_guest(request, db)
    user = guest or User()
    user.display_name = body.display_name
    user.username = body.username
    user.password_hash = await asyncio.to_thread(hasher.hash, body.password)
    user.is_guest = False
    db.add(user)
    try:
        return await issue_session(db, request, response, user)
    except IntegrityError:
        await db.rollback()
        raise APIError(409, "USERNAME_TAKEN", "Tên đăng nhập đã được sử dụng.") from None


@router.post("/password", response_model=UserOutput)
async def password_login(
    body: Credentials, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    await throttle(db, request, "login-ip", limit=60)
    await throttle(db, request, "login", body.username, limit=10)
    user = await db.scalar(select(User).where(User.username == body.username))
    if not await asyncio.to_thread(
        verify_password, user.password_hash if user else None, body.password
    ):
        raise APIError(401, "INVALID_CREDENTIALS", "Tên đăng nhập hoặc mật khẩu không đúng.")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = await asyncio.to_thread(hasher.hash, body.password)
    return await issue_session(db, request, response, user)


@router.post("/guest", response_model=UserOutput, status_code=201)
async def guest_login(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    if request.headers.get("X-G6-Share-Account-Required") == "1":
        raise APIError(403, "ACCOUNT_REQUIRED", "Vui lòng đăng ký hoặc đăng nhập.")
    await throttle(db, request, "guest", limit=20)
    user = await optional_guest(request, db)
    if user is None:
        user = User(display_name="Khách", is_guest=True)
        db.add(user)
    return await issue_session(db, request, response, user)


@router.put("/password", response_model=UserOutput)
async def change_password(
    body: PasswordChange,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await throttle(db, request, "password-change", str(user.id), limit=10)
    if user.is_guest or not user.password_hash:
        raise APIError(403, "REGISTRATION_REQUIRED", "Hãy tạo tài khoản trước.")
    if user.password_hash and not await asyncio.to_thread(
        verify_password, user.password_hash, body.current_password or ""
    ):
        raise APIError(401, "INVALID_CREDENTIALS", "Mật khẩu hiện tại không đúng.")
    user.username = body.username
    user.password_hash = await asyncio.to_thread(hasher.hash, body.password)
    # Password changes invalidate this account's other devices, never other accounts.
    await db.execute(delete(SessionRecord).where(SessionRecord.user_id == user.id))
    try:
        return await issue_session(db, request, response, user)
    except IntegrityError:
        await db.rollback()
        raise APIError(409, "USERNAME_TAKEN", "Tên đăng nhập đã được sử dụng.") from None
