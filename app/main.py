import asyncio
import json
import logging
import sys
from pathlib import Path
import uvicorn
from app.bot import dp
from app.web import app as web_app
from config import settings, utcnow
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
        data = json.loads(CHATS_FILE.read_text())
        if data and isinstance(data[0], str):
            config_chats = data
        else:
            config_chats = [c["url"] for c in data if c.get("active", True)]
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
        from aiogram.types import BotCommand
        from app.bot import bot, notification_worker
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Запуск / главное меню"),
                BotCommand(command="myleads", description="Мои активные лиды"),
                BotCommand(command="mystats", description="Моя статистика за 7 дней"),
                BotCommand(command="kpi", description="Мои KPI и цели"),
                BotCommand(command="top", description="Топ лидов по Score"),
                BotCommand(command="overdue", description="Просроченные лиды"),
                BotCommand(command="deals", description="Последние сделки"),
                BotCommand(command="teamstats", description="Статистика команды (админ)"),
                BotCommand(command="search", description="Поиск по лидам"),
                BotCommand(command="help", description="Справка по командам"),
            ])
        except Exception as e:
            logger.warning("set_my_commands failed: %s", e)
        worker = asyncio.create_task(notification_worker())
        await dp.start_polling(bot)
        worker.cancel()
    except Exception as e:
        logger.error("Bot failed: %s", e)


async def run_anomaly_checker():
    """Periodically check for anomalies and send notifications."""
    from app.bot import check_and_notify_anomalies
    while True:
        try:
            await asyncio.sleep(3600)
            await check_and_notify_anomalies()
            logger.info("Anomaly check completed")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Anomaly checker error: %s", e)
        await asyncio.sleep(300)


async def run_scraper_monitor():
    """Live-mode scraper: polls every 2 minutes for new leads."""
    from app.scrapers import VKScraper, AvitoScraper, CIANScraper, ForumHouseScraper
    from app.models import LeadSource, Lead, User, Tenant
    from app.repositories import LeadRepository
    from app.analyzer import analyze_message
    from app.config_manager import TenantConfig
    from datetime import datetime

    # Per-source cooldowns (seconds) — avoid hammering sites
    COOLDOWNS = {
        "vk": 120,          # 2 min — API, fast
        "avito": 180,       # 3 min — web scraping, medium
        "cian": 300,        # 5 min — web scraping, slow
        "forumhouse": 120,  # 2 min — forum, fast
    }
    last_run = {}  # source_id -> timestamp

    while True:
        try:
            await asyncio.sleep(120)  # Main loop: 2 minutes

            async with async_session() as session:
                result = await session.execute(
                    select(LeadSource).where(LeadSource.is_active == True)
                )
                sources = result.scalars().all()

            now = datetime.utcnow()

            for source in sources:
                try:
                    # Check cooldown
                    cooldown = COOLDOWNS.get(source.name, 180)
                    last = last_run.get(source.id)
                    if last and (now - last).total_seconds() < cooldown:
                        continue

                    config = source.config or {}
                    tenant_id = source.tenant_id

                    # Load tenant config for scoring
                    tenant_config = TenantConfig(config)

                    scraper_map = {
                        "vk": VKScraper,
                        "avito": AvitoScraper,
                        "cian": CIANScraper,
                        "forumhouse": ForumHouseScraper,
                    }

                    ScraperClass = scraper_map.get(source.name)
                    if not ScraperClass:
                        continue

                    scraper = ScraperClass(config)
                    last_run[source.id] = now

                    city = config.get("city", "")
                    leads = await scraper.monitor(
                        queries=config.get("queries", []),
                        cities=[city] if city else [],
                    )

                    new_leads_count = 0
                    async with async_session() as session:
                        leads_repo = LeadRepository(session, tenant_id)

                        for scraped_lead in leads:
                            # Dedup by source_id
                            source_id_text = scraped_lead.source_id[:50]
                            existing = await session.execute(
                                select(Lead).where(
                                    Lead.tenant_id == tenant_id,
                                    Lead.message_text.ilike(f"%{source_id_text}%"),
                                )
                            )
                            if existing.scalar_one_or_none():
                                continue

                            analysis = await analyze_message(
                                scraped_lead.text,
                                f"{source.name}:{scraped_lead.source}",
                                tenant_config=tenant_config,
                                tenant_id=tenant_id,
                            )

                            if not analysis.get("is_lead"):
                                continue
                            if analysis["lead_score"] < tenant_config.min_lead_score:
                                continue

                            await leads_repo.create(
                                message_text=scraped_lead.text[:5000],
                                chat_title=f"[{source.display_name or source.name}] {scraped_lead.city or 'N/A'}",
                                chat_username=scraped_lead.source_url,
                                lead_score=analysis["lead_score"],
                                urgency=scraped_lead.urgency or analysis.get("urgency", "low"),
                                reason=analysis.get("reason", ""),
                                recommended_message=analysis.get("recommended_message", ""),
                                user_id=scraped_lead.author_id or None,
                                username=scraped_lead.author_username or None,
                                first_name=scraped_lead.author_name or None,
                            )
                            new_leads_count += 1

                        await session.commit()

                    async with async_session() as session:
                        result = await session.execute(
                            select(LeadSource).where(LeadSource.id == source.id)
                        )
                        src = result.scalar_one_or_none()
                        if src:
                            src.leads_found = (src.leads_found or 0) + new_leads_count
                            src.last_synced = utcnow()
                            await session.commit()

                    if new_leads_count > 0:
                        logger.info("Scraper %s: +%d new leads — notifying", source.name, new_leads_count)
                        # Notify owner about scraper leads (they bypass the monitor pipeline)
                        try:
                            from app.models import Lead as LeadM
                            from app.bot import send_lead_notification
                            async with async_session() as ns:
                                recent = (await ns.execute(
                                    select(LeadM).where(
                                        LeadM.chat_title.like(f"[{source.display_name or source.name}]%"),
                                        LeadM.is_notified == 0,
                                    ).order_by(LeadM.created_at.desc()).limit(new_leads_count)
                                )).scalars().all()
                            for l in recent:
                                await send_lead_notification(l)
                        except Exception as ne:
                            logger.warning("Scraper notify failed: %s", ne)

                except Exception as e:
                    logger.error("Scraper %s failed: %s", source.name, e)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Scraper monitor error: %s", e)
        await asyncio.sleep(10)


async def run_reminder_checker():
    """Periodically check for unprocessed leads and send reminders."""
    from app.bot import check_and_send_reminders, auto_assign_leads
    while True:
        try:
            await auto_assign_leads()
            await check_and_send_reminders()
            logger.info("Reminder + auto-assign check completed")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Reminder checker error: %s", e)
        await asyncio.sleep(1800)  # every 30 minutes


async def run_webhook_dispatcher():
    """Periodically fire pending webhooks."""
    import hashlib
    import hmac
    import json as _json
    import aiohttp
    from app.models import Webhook as WebhookModel
    from app.repositories import WebhookRepository

    while True:
        try:
            await asyncio.sleep(60)
            async with async_session() as session:
                result = await session.execute(
                    select(WebhookModel).where(WebhookModel.is_active == True)
                )
                webhooks = result.scalars().all()

            # Webhooks are fired on events, this just cleans up stale ones
            for wh in webhooks:
                if wh.fail_count and wh.fail_count > 10:
                    wh.is_active = False
                    logger.warning("Deactivated webhook %d (fail_count=%d)", wh.id, wh.fail_count)
            if webhooks:
                await session.commit()

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Webhook dispatcher error: %s", e)
        await asyncio.sleep(300)


async def run_action_log_cleanup():
    """Daily purge of audit log entries older than the retention window (90 days)."""
    from datetime import timedelta
    from sqlalchemy import delete
    from app.models import ActionLog
    while True:
        try:
            cutoff = utcnow() - timedelta(days=90)
            async with async_session() as session:
                result = await session.execute(
                    delete(ActionLog).where(ActionLog.created_at < cutoff)
                )
                await session.commit()
                if result.rowcount:
                    logger.info("Action log cleanup: removed %d entries older than %s", result.rowcount, cutoff.date())
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Action log cleanup error: %s", e)
        await asyncio.sleep(86400)  # daily


async def run_db_backup():
    """Daily SQLite backup (online, via backup API), keep last 7 copies."""
    import sqlite3
    from pathlib import Path
    while True:
        try:
            url = settings.get_database_url()
            if "sqlite" in url:
                db_path = url.split("///", 1)[-1]
                src_file = Path(db_path)
                if not src_file.is_absolute():
                    src_file = Path(__file__).resolve().parent.parent / db_path
                src_file = src_file.resolve()
                if src_file.exists():
                    backup_dir = src_file.parent / "backups"
                    backup_dir.mkdir(exist_ok=True)
                    stamp = utcnow().strftime("%Y%m%d_%H%M%S")
                    dest = backup_dir / f"lead_hunter_{stamp}.db"
                    src_conn = sqlite3.connect(str(src_file))
                    dst_conn = sqlite3.connect(str(dest))
                    with dst_conn:
                        src_conn.backup(dst_conn)
                    dst_conn.close()
                    src_conn.close()

                    backups = sorted(backup_dir.glob("lead_hunter_*.db"))
                    for old in backups[:-7]:
                        old.unlink(missing_ok=True)
                    logger.info("DB backup created: %s (%s)", dest.name,
                                f"{dest.stat().st_size // 1024} KB")
                else:
                    logger.warning("DB backup skipped: %s not found", db_path)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("DB backup error: %s", e)
        await asyncio.sleep(86400)  # daily


async def run_filter_stats_flush():
    """Flush in-memory rejection counters to filter_stats every 5 minutes."""
    from sqlalchemy import and_
    from app.models import FilterStat
    from app.analyzer import drain_reject_stats
    while True:
        try:
            await asyncio.sleep(300)
            rows = drain_reject_stats()
            if rows:
                day = utcnow().date()
                async with async_session() as session:
                    for (tid, chat, src, reason), cnt in rows:
                        tenant_cond = FilterStat.tenant_id == tid if tid else FilterStat.tenant_id.is_(None)
                        res = await session.execute(
                            select(FilterStat).where(and_(
                                tenant_cond,
                                FilterStat.day == day,
                                FilterStat.chat_title == chat,
                                FilterStat.source == src,
                                FilterStat.reason == reason,
                            ))
                        )
                        obj = res.scalar_one_or_none()
                        if obj:
                            obj.cnt += cnt
                        else:
                            session.add(FilterStat(
                                tenant_id=tid or None, day=day, chat_title=chat,
                                source=src, reason=reason, cnt=cnt))
                    await session.commit()
                    logger.info("Filter stats flushed: %d buckets", len(rows))
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Filter stats flush error: %s", e)


async def run_weekly_report():
    """Send weekly owner digest every Monday 09:00 UTC."""
    from datetime import timedelta
    from app.bot import send_weekly_reports
    while True:
        try:
            now = utcnow()
            days_ahead = (0 - now.weekday()) % 7  # 0 = Monday
            target = (now + timedelta(days=days_ahead)).replace(
                hour=9, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=7)
            await asyncio.sleep((target - now).total_seconds())
            await send_weekly_reports()
            logger.info("Weekly reports sent")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Weekly report error: %s", e)
            await asyncio.sleep(3600)


async def run_chat_discovery():
    """Daily discovery of new public ЖК chats per tenant with active session."""
    from datetime import timedelta
    from app.models import TelegramSession
    from app.chat_discovery import discover_chats_for_tenant
    from app.config_manager import TenantConfig
    while True:
        try:
            async with async_session() as session:
                rows = (await session.execute(
                    select(Tenant, TelegramSession)
                    .join(TelegramSession, TelegramSession.tenant_id == Tenant.id)
                    .where(Tenant.is_active == True,
                           TelegramSession.is_active == True,
                           TelegramSession.is_authorized == True)
                )).all()

            for tenant, db_session in rows:
                try:
                    tc = TenantConfig(raw_config=tenant.config or {})
                    city = tc._config.get("city", "")
                    await discover_chats_for_tenant(
                        tenant.id, db_session.session_name,
                        city=city, monitored=tc.monitored_chats)
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error("Chat discovery failed for tenant %s: %s", tenant.id, e)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Chat discovery error: %s", e)
        await asyncio.sleep(86400)  # daily


def run_web():
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    server = uvicorn.Server(config)
    return server.serve()


async def run_ml_retrain():
    """Periodically retrain ML model on historical outcomes."""
    import asyncio
    while True:
        try:
            await asyncio.sleep(21600)  # Every 6 hours
            from app.ml_scorer import retrain_model
            await retrain_model()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("ML retrain error: %s", e)


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
    tasks.append(run_action_log_cleanup())
    tasks.append(run_db_backup())
    tasks.append(run_filter_stats_flush())

    if settings.bot_token:
        tasks.append(run_bot())
        tasks.append(run_anomaly_checker())
        tasks.append(run_reminder_checker())
        tasks.append(run_scraper_monitor())
        tasks.append(run_ml_retrain())
        tasks.append(run_weekly_report())
        tasks.append(run_chat_discovery())
        logger.info("Bot + anomaly checker + reminders + scrapers + ML retrain + weekly report + chat discovery: started")
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
