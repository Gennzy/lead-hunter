import asyncio
import secrets
import string
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models import Base, Tenant, User
from app.auth import hash_password
from config import settings, KEYWORDS_LIST, NOISE_KEYWORDS_LIST


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


async def init_db():
    _url = settings.get_database_url()
    _ekw = {"echo": False}
    if "sqlite" in _url:
        _ekw["connect_args"] = {"timeout": 30}
    engine = create_async_engine(_url, **_ekw)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        # Create default tenant if not exists
        result = await session.execute(select(Tenant).where(Tenant.slug == "default"))
        if not result.scalar_one_or_none():
            default_tenant = Tenant(
                name="Default",
                slug="default",
                city="Санкт-Петербург",
                config={
                    "keywords": KEYWORDS_LIST,
                    "noise_keywords": NOISE_KEYWORDS_LIST,
                    "min_lead_score": settings.min_lead_score,
                    "system_prompt": "",
                },
            )
            session.add(default_tenant)
            await session.flush()
            print(f"[db_init] Created default tenant (id={default_tenant.id})")

            # Create super_admin with generated password
            admin_result = await session.execute(
                select(User).where(User.username == "admin")
            )
            if not admin_result.scalar_one_or_none():
                admin_password = _generate_password()
                admin = User(
                    username="admin",
                    password_hash=hash_password(admin_password),
                    full_name="Super Admin",
                    role="super_admin",
                    tenant_id=None,
                    must_change_password=True,
                )
                session.add(admin)
                print("=" * 60)
                print("[db_init] SUPER ADMIN CREATED")
                print(f"  Username: admin")
                print(f"  Password: {admin_password}")
                print("  MUST CHANGE PASSWORD ON FIRST LOGIN")
                print("=" * 60)
            await session.commit()
        else:
            print("[db_init] Database already initialized.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(init_db())
