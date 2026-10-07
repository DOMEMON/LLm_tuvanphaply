"""Read-only database diagnostics: python -m app.doctor [--config-only]."""

import argparse
import asyncio
import os

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import BACKEND_ROOT, get_settings


def diagnose_exception(exc: BaseException) -> tuple[str, str]:
    seen = set()
    item = exc
    while item is not None and id(item) not in seen:
        seen.add(id(item))
        code = getattr(item, "sqlstate", None)
        if code == "28P01":
            return "DB_AUTH_FAILED", "Sai mat khau. Xem README: dat lai mat khau, giu volume."
        if code == "3D000":
            return "DB_NOT_FOUND", "Database chua ton tai. Tao DB_NAME tren server da chon."
        if code == "28000":
            return "DB_AUTH_FAILED", "Kiem tra DB_USER va quyen ket noi PostgreSQL."
        if isinstance(item, (ConnectionRefusedError, OSError)):
            return "DB_NETWORK_ERROR", "Kiem tra server/container, DB_HOST va cong DB_PORT."
        item = getattr(item, "orig", None) or item.__cause__ or item.__context__
    return "DB_CHECK_FAILED", "Khong kiem tra duoc database. Xem log PostgreSQL va README."


async def check_database(url, timeout: float = 5) -> tuple[bool, str, str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                revision_table = await connection.scalar(
                    text("SELECT to_regclass('alembic_version')")
                )
                revision = None
                if revision_table:
                    revision = await connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
        return (
            True,
            "DB_OK",
            f"Ket noi thanh cong. Migration: {revision or 'chua co; chay upgrade head'}.",
        )
    except TimeoutError:
        return False, "DB_TIMEOUT", "Qua 5 giay. Kiem tra host/port va database server."
    except Exception as exc:
        code, hint = diagnose_exception(exc)
        return False, code, hint
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args()
    try:
        settings = get_settings()
        url = settings.connection_url
    except (ValidationError, ValueError):
        print("CONFIG_ERROR: Kiem tra .env: DB_PORT la so, CORS_ORIGINS la JSON array.")
        print("DATABASE_URL can dung postgresql+asyncpg://...; hoac bo no de dung DB_*.")
        return 2
    print(f"Config file: {BACKEND_ROOT / '.env'}")
    print(
        "Config mode: "
        + (
            "DATABASE_URL"
            if settings.database_url and settings.database_url.get_secret_value().strip()
            else "DB_*"
        )
    )
    # Do not print URL query parameters, which might themselves contain secrets.
    print(f"Target: host={url.host!r} port={url.port or 5432} database={url.database!r}")
    print("Password: [hidden]")
    overrides = sorted(
        name
        for name in ("DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
        if name in os.environ
    )
    if overrides:
        print("Shell overrides (names only): " + ", ".join(overrides))
    if args.config_only:
        print("CONFIG_OK: Cu phap hop le; chua kiem tra ket noi/mat khau.")
        return 0
    ok, code, hint = asyncio.run(check_database(url))
    print(f"{code}: {hint}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
