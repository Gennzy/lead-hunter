import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Request
from fastapi.responses import RedirectResponse
from jose import jwt, JWTError
import bcrypt
from sqlalchemy import select
from app.models import User, Tenant, async_session
from config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "lead_hunter_token"


def _generate_password(length: int = 16) -> str:
    """Generate a secure random password with mixed characters."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in "!@#$%^&*" for c in password)
        ):
            return password


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_token(user_id: int, tenant_id: int | None = None) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
    }
    if tenant_id is not None:
        payload["tid"] = tenant_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = int(payload["sub"])
        tenant_id = payload.get("tid")
        return {"user_id": user_id, "tenant_id": tenant_id}
    except (JWTError, KeyError, ValueError):
        return None


async def get_current_user(request: Request) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    data = decode_token(token)
    if not data:
        return None
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == data["user_id"]))
        user = result.scalar_one_or_none()
        if not user:
            return None
        # Block access if user's tenant is deactivated (except super_admin with no tenant)
        if user.tenant_id is not None:
            tenant_result = await session.execute(
                select(Tenant).where(Tenant.id == user.tenant_id, Tenant.is_active == True)
            )
            if not tenant_result.scalar_one_or_none():
                return None
        return user


async def seed_admin():
    async with async_session() as session:
        result = await session.execute(select(User).where(User.role.in_(["super_admin", "admin"])))
        if result.scalars().first():
            return
        admin_password = _generate_password()
        admin = User(
            username="admin",
            password_hash=hash_password(admin_password),
            full_name="Super Admin",
            role="super_admin",
            must_change_password=True,
        )
        session.add(admin)
        await session.commit()
        logger.warning(
            "SUPER ADMIN CREATED (username=admin, password=%s). "
            "MUST CHANGE PASSWORD ON FIRST LOGIN.",
            admin_password,
        )
