import asyncio
import json
import logging
import sys
from pathlib import Path
import uvicorn
from app.bot import dp
from app.web import app as web_app
from config import settings
from app.models import Tenant, async_session
from sqlalchemy import select
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CHATS_FILE = Path("config_chats.json")

# Global registry of active monitor tasks keyed by tenant_id
_monitor_tasks: dict[int, asyncio.Task] = {}


def cancel_monitor_for_tenant(tenant_id: int) -> bool:
    """Cancel the running monitor task for a tenant. Returns True if cancelled."""
    task = _monitor_tasks.pop(tenant_id, None)
    if task and not task.done():
        task.cancel()
        logger.info("Cancelled monitor task for tenant %d", tenant_id)
        return True
    return False


def get_monitored_chats() -> list[str]:
    config_chats = []
    if CHATS_FILE.exists():
        config_chats = json.loads(CHATS_FILE.read_text())
    env_chats = settings.get_monitored_chats()
    return list(set(config_chats + env_chats))


async def run_monitor_for_tenant(tenant_id: int, session_name: str):
    """Start monitor for a single tenant with auto-restart and exponential backoff."""
    from app.monitor import start_monitor
    from app.telegram_factory import client_factory
    from app.config_manager import TenantConfig

    backoff_seconds = 5
    max_backoff = 300  # 5 minutes max

    while True:
        try:
            # Get tenant config from DB
            async with async_session() as session:
                result = await session.execute(
                    select(Tenant).where(Tenant.id == tenant_id)
                )
                tenant = result.scalar_one_or_none()
                if not tenant or not tenant.is_active:
                    logger.warning("Tenant %d not found or inactive, stopping monitor", tenant_id)
                    return

            tenant_config = TenantConfig(raw_config=tenant.config)

            chats = tenant_config.chats
            if not chats:
                chats = get_monitored_chats()

            if not chats:
                logger.warning("No chats configured for tenant %d, stopping monitor", tenant_id)
                return

            # Create and start client via factory
            client = client_factory.create_client(tenant_id, session_name)
            if not await client_factory.start_client(tenant_id):
                logger.error("Failed to start client for tenant %d, retrying in %ds",
                             tenant_id, backoff_seconds)
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, max_backoff)
                continue

            logger.info("Starting monitor for tenant %d, chats: %s", tenant_id, chats)
            backoff_seconds = 5  # Reset backoff on successful connection
            await start_monitor(client, chats, tenant_config, tenant_id)

            # If start_monitor returns normally (shouldn't happen), restart
            logger.warning("Monitor for tenant %d exited normally, restarting", tenant_id)

        except asyncio.CancelledError:
            logger.info("Monitor for tenant %d cancelled", tenant_id)
            return
        except Exception as e:
            logger.error("Monitor failed for tenant %d: %s, retrying in %ds",
                         tenant_id, e, backoff_seconds)

        await asyncio.sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2, max_backoff)


async def run_monitors():
    """Start monitors for all active tenants with sessions."""
    from app.telegram_factory import client_factory

    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.is_active == True)
        )
        tenants = result.scalars().all()

    tasks = []
    for tenant in tenants:
        # Check if tenant has an authorized session
        from app.models import TelegramSession
        async with async_session() as session:
            result = await session.execute(
                select(TelegramSession).where(
                    TelegramSession.tenant_id == tenant.id,
                    TelegramSession.is_active == True,
                    TelegramSession.is_authorized == True
                )
            )
            db_session = result.scalar_one_or_none()

        if db_session:
            task = asyncio.create_task(
                run_monitor_for_tenant(tenant.id, db_session.session_name),
                name=f"monitor-tenant-{tenant.id}"
            )
            _monitor_tasks[tenant.id] = task
            tasks.append(task)
            logger.info("Monitor queued for tenant %d (%s)", tenant.id, tenant.name)
        else:
            logger.info("No active session for tenant %d, skipping monitor", tenant.id)

    if not tasks:
        logger.warning("No tenants with active sessions to monitor")
        return

    # Use return_exceptions=True to isolate failures — one tenant's crash
    # must not cancel monitors for other tenants.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error("Monitor task finished with exception: %s", result)


async def run_bot():
    try:
        from app.bot import bot
        await dp.start_polling(bot)
    except Exception as e:
        logger.error("Bot failed: %s", e)


def run_web():
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    server = uvicorn.Server(config)
    return server.serve()


async def main():
    has_api = settings.telegram_api_id and settings.telegram_api_id != "0" and settings.telegram_api_hash

    # Create tables if they don't exist
    from app.models import Base, engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured.")

    logger.info("=== Lead Hunter ===")
    logger.info("Web panel: http://127.0.0.1:8000")

    tasks = [run_web()]

    if settings.bot_token:
        tasks.append(run_bot())
        logger.info("Bot: started")
    else:
        logger.warning("Bot: no BOT_TOKEN, skipped")

    if has_api:
        tasks.append(run_monitors())
        logger.info("Monitor: starting for active tenants")
    else:
        logger.warning("Monitor: no TELEGRAM_API_ID/HASH, skipped")
        logger.info("To enable monitor, fill TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
