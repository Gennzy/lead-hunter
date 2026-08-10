import html
import json
import logging
import re
import random
import time
from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from app.models import Lead, Tenant, async_session
from app.config_manager import TenantConfig
from app.repositories import ActionLogRepository, ManagerActionRepository
from config import settings, utcnow

logger = logging.getLogger(__name__)

bot = Bot(token=settings.bot_token)
dp = Dispatcher()

# Pending link verifications: {chat_id: {"username": str, "code": str, "expires": float}}
_pending_links: dict[int, dict] = {}


def _cleanup_pending_links():
    """Remove expired pending link entries."""
    now = time.time()
    expired = [cid for cid, data in _pending_links.items() if now >= data["expires"]]
    for cid in expired:
        del _pending_links[cid]


async def _get_tenant_config(tenant_id: int | None) -> TenantConfig:
    """Load tenant config from database."""
    if tenant_id is None:
        return TenantConfig()
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = result.scalar_one_or_none()
        if tenant and tenant.config:
            return TenantConfig(tenant.config)
    return TenantConfig()


def _lead_text(lead: Lead) -> str:
    # Escape all user-controlled fields — this message is sent with
    # parse_mode="HTML", so raw '<', '>', '&' from a chat/username/message
    # will otherwise break Telegram's HTML parser and silently kill the
    # notification (send_lead_notification swallows the exception).
    chat_title = html.escape(lead.chat_title or "")
    first_name = html.escape(lead.first_name or "")
    last_name = html.escape(lead.last_name or "")
    username = html.escape(lead.username or "нет username")
    reply_to_text = html.escape((lead.reply_to_text or "")[:300])
    message_text = html.escape((lead.message_text or "")[:500])
    reason = html.escape(lead.reason or "")

    urgency_label = {"high": "Высокая", "medium": "Средняя", "low": "Низкая"}.get(lead.urgency, "")

    from datetime import datetime
    now = utcnow()
    created = lead.created_at.replace(tzinfo=None) if lead.created_at else now
    diff = now - created
    mins = int(diff.total_seconds() / 60)
    if mins < 1:
        time_str = "только что"
    elif mins < 60:
        time_str = f"{mins} мин назад"
    elif mins < 1440:
        time_str = f"{mins // 60} ч назад"
    else:
        time_str = f"{mins // 1440} дн назад"

    text = (
        f"<b>Новый лид #{lead.id}</b>\n\n"
        f"<b>{chat_title}</b>\n"
        f"{first_name} {last_name} (@{username})\n"
        f"{time_str}\n\n"
    )
    if lead.reply_to_text:
        text += f"<i>{reply_to_text[:150]}...</i>\n\n"
    text += (
        f"<i>{message_text[:300]}</i>\n\n"
        f"Score: <b>{lead.lead_score}</b> | {urgency_label}\n"
        f"{reason}\n"
    )
    return text


def _lead_keyboard(lead: Lead, is_manager: bool = False) -> InlineKeyboardMarkup:
    buttons = []

    buttons.append(
        [
            InlineKeyboardButton(
                text="Открыть в панели",
                url=f"{settings.get_site_url()}/leads/{lead.id}",
            ),
        ]
    )

    profile = lead.profile_url()
    if profile and profile != "#":
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Профиль Telegram",
                    url=profile,
                ),
            ],
        )

    if lead.chat_username and lead.message_id:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Исходное сообщение",
                    url=f"https://t.me/{lead.chat_username}/{lead.message_id}",
                ),
            ]
        )

    if lead.status == "new" and not lead.assigned_to:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Взять лид",
                    callback_data=f"take_lead:{lead.id}",
                ),
            ]
        )

    if lead.status in ("new", "contacted", "in_progress", "missed_call"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Контактировали",
                    callback_data=f"contacted:{lead.id}",
                ),
                InlineKeyboardButton(
                    text="В работе",
                    callback_data=f"in_progress:{lead.id}",
                ),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Недозвон",
                    callback_data=f"missed_call:{lead.id}",
                ),
                InlineKeyboardButton(
                    text="Сделка",
                    callback_data=f"deal:{lead.id}",
                ),
            ]
        )

    if lead.status not in ("deal", "deleted", "archive"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Напомнить через 24ч",
                    callback_data=f"followup:{lead.id}",
                ),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Запланировать замер",
                    callback_data=f"schedule:{lead.id}",
                ),
            ]
        )

    # Manager action tracking buttons (only for assigned manager)
    if is_manager and lead.assigned_to and lead.status in ("contacted", "in_progress"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ Первый контакт",
                    callback_data=f"action_first_contact:{lead.id}",
                ),
                InlineKeyboardButton(
                    text="💬 Клиент ответил",
                    callback_data=f"action_client_responded:{lead.id}",
                ),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="📅 Встреча назначена",
                    callback_data=f"action_appointment:{lead.id}",
                ),
            ]
        )

    if lead.status not in ("deal", "deleted", "archive"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Архив",
                    callback_data=f"archive:{lead.id}",
                ),
                InlineKeyboardButton(
                    text="Не лид",
                    callback_data=f"not_lead:{lead.id}",
                ),
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


_notification_timestamps = []
NOTIFICATION_RATE_LIMIT = 5
NOTIFICATION_RATE_WINDOW = 60

async def send_lead_notification(lead: Lead):
    try:
        import asyncio, time
        now = time.time()
        _notification_timestamps[:] = [t for t in _notification_timestamps if now - t < NOTIFICATION_RATE_WINDOW]
        if len(_notification_timestamps) >= NOTIFICATION_RATE_LIMIT:
            logger.warning("Notification rate limit reached, delaying lead %d", lead.id)
            await asyncio.sleep(3)
            _notification_timestamps[:] = [t for t in _notification_timestamps if time.time() - t < NOTIFICATION_RATE_WINDOW]
            if len(_notification_timestamps) >= NOTIFICATION_RATE_LIMIT:
                logger.error("Notification rate limit still exceeded, skipping lead %d", lead.id)
                return

        _notification_timestamps.append(time.time())

        tenant_config = await _get_tenant_config(lead.tenant_id)
        owner_chat_id = tenant_config.owner_chat_id

        text = _lead_text(lead)

        # Send to assigned manager if they have telegram_id
        manager_chat_id = None
        if lead.assigned_to:
            try:
                from app.models import User
                async with async_session() as session:
                    user = await session.get(User, lead.assigned_to)
                    if user and user.telegram_id:
                        manager_chat_id = user.telegram_id
            except Exception:
                pass

        # Send to manager first
        if manager_chat_id:
            kb = _lead_keyboard(lead, is_manager=True)
            try:
                await bot.send_message(
                    chat_id=manager_chat_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Manager notification failed for lead %d", lead.id)

        # Send to owner (skip if owner is the manager)
        if owner_chat_id and owner_chat_id != manager_chat_id:
            kb = _lead_keyboard(lead, is_manager=False)
            try:
                await bot.send_message(
                    chat_id=owner_chat_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("HTML notification failed for lead %d — retrying as plain text", lead.id)
                plain_text = re.sub(r"<[^>]+>", "", text)
                await bot.send_message(chat_id=owner_chat_id, text=plain_text, reply_markup=kb)

        async with async_session() as session:
            await session.execute(
                update(Lead).where(Lead.id == lead.id).values(is_notified=1)
            )
            await session.commit()

        await send_push_notifications(lead)

    except Exception:
        logger.exception("Failed to send notification")


async def send_push_notifications(lead):
    try:
        from app.models import User
        if not settings.vapid_private_key:
            return

        async with async_session() as session:
            result = await session.execute(
                select(User).where(
                    User.tenant_id == lead.tenant_id,
                    User.is_active == True,
                    User.push_subscriptions.isnot(None),
                )
            )
            users = result.scalars().all()

        title = f"Новый лид #{lead.id}"
        body = f"{lead.first_name or ''} {lead.last_name or ''} — Score {int(lead.lead_score)}"
        url = f"/leads/{lead.id}"

        import json
        from pywebpush import webpush, WebPushException

        for user in users:
            subs = user.push_subscriptions or []
            for sub in subs[:]:
                try:
                    webpush(
                        subscription_info=sub,
                        data=json.dumps({"title": title, "body": body, "url": url}),
                        vapid_private_key=settings.vapid_private_key,
                        vapid_claims={"sub": settings.vapid_email or "mailto:admin@leadhunter.local"},
                    )
                except WebPushException:
                    subs.remove(sub)
                except Exception:
                    pass
            if subs != (user.push_subscriptions or []):
                user.push_subscriptions = subs
                await session.commit()

    except Exception:
        logger.debug("Push notification skipped")


async def _check_lead_access(lead, user_id: int) -> bool:
    """Check if user can act on this lead (owner or assigned manager)."""
    if user_id == (await _get_tenant_config(lead.tenant_id)).owner_chat_id:
        return True
    if lead.assigned_to:
        try:
            from app.models import User
            async with async_session() as session:
                user = await session.get(User, lead.assigned_to)
                if user and user.telegram_id == user_id:
                    return True
        except Exception:
            pass
    return False


async def _update_lead_status(lead_id: int, new_status: str, callback: CallbackQuery, label: str):
    """Helper to update lead status from bot callback."""
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return False
        if not await _check_lead_access(lead, callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return False
        old_status = lead.status
        lead.status = new_status
        if new_status == "contacted" and old_status == "new":
            lead.last_responded_at = utcnow()
        await ActionLogRepository(session, lead.tenant_id).log(
            None, "status_change", lead_id=lead_id,
            meta={"from": old_status, "to": new_status, "via": "telegram_bot"},
        )
        await session.commit()
    await callback.answer(label)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(callback.message.text + f"\n\n<b>{label}</b>", parse_mode="HTML")
    return True


@dp.callback_query(F.data.startswith("take_lead:"))
async def cb_take_lead(callback: CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        if lead.assigned_to:
            await callback.answer("Лид уже назначен", show_alert=True)
            return
        # Find user by telegram_id
        from app.models import User
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Привяжите аккаунт командой /link", show_alert=True)
            return
        lead.assigned_to = user.id
        lead.status = "contacted"
        lead.last_responded_at = utcnow()
        await ActionLogRepository(session, lead.tenant_id).log(
            user.id, "status_change", lead_id=lead_id,
            meta={"from": "new", "to": "contacted", "via": "telegram_bot", "action": "take_lead"},
        )
        await session.commit()
    await callback.answer("Лид взят!", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(callback.message.text + "\n\n<b>Лид взят менеджером</b>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("contacted:"))
async def cb_contacted(callback: CallbackQuery):
    await _update_lead_status(int(callback.data.split(":")[1]), "contacted", callback, "Контактировали")


@dp.callback_query(F.data.startswith("in_progress:"))
async def cb_in_progress(callback: CallbackQuery):
    await _update_lead_status(int(callback.data.split(":")[1]), "in_progress", callback, "В работе")


@dp.callback_query(F.data.startswith("missed_call:"))
async def cb_missed_call(callback: CallbackQuery):
    await _update_lead_status(int(callback.data.split(":")[1]), "missed_call", callback, "Недозвон")


@dp.callback_query(F.data.startswith("deal:"))
async def cb_deal(callback: CallbackQuery):
    await _update_lead_status(int(callback.data.split(":")[1]), "deal", callback, "Сделка!")


@dp.callback_query(F.data.startswith("archive:"))
async def cb_archive(callback: CallbackQuery):
    await _update_lead_status(int(callback.data.split(":")[1]), "archive", callback, "Архивировано")


@dp.callback_query(F.data.startswith("not_lead:"))
async def cb_not_lead(callback: CallbackQuery):
    await _update_lead_status(int(callback.data.split(":")[1]), "not_interested", callback, "Не лид")


@dp.callback_query(F.data.startswith("followup:"))
async def cb_followup(callback: CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        if not await _check_lead_access(lead, callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from app.models import User
        from app.repositories import FollowUpRepository
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Привяжите аккаунт", show_alert=True)
            return
        from datetime import timedelta
        fu_repo = FollowUpRepository(session, lead.tenant_id)
        await fu_repo.create(
            lead_id=lead_id,
            user_id=user.id,
            scheduled_at=utcnow() + timedelta(hours=24),
            note="Автоматическое напоминание через 24ч",
        )
        await session.commit()
    await callback.answer("Напоминание через 24ч!", show_alert=True)


@dp.callback_query(F.data.startswith("schedule:"))
async def cb_schedule(callback: CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        if not await _check_lead_access(lead, callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from app.models import User
        from app.repositories import AppointmentRepository
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Привяжите аккаунт", show_alert=True)
            return
        from datetime import timedelta
        apt_repo = AppointmentRepository(session, lead.tenant_id)
        await apt_repo.create(
            lead_id=lead_id,
            user_id=user.id,
            title=f"Замер — {lead.first_name or ''} {lead.last_name or ''}".strip(),
            scheduled_at=utcnow() + timedelta(days=3),
            description="Запланировано из Telegram бота",
        )
        await session.commit()
    await callback.answer("Замер запланирован через 3 дня!", show_alert=True)


# ==================== MANAGER ACTION TRACKING ====================

@dp.callback_query(F.data.startswith("action_first_contact:"))
async def cb_first_contact(callback: CallbackQuery):
    """Manager confirms first contact with client."""
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        if not await _check_lead_access(lead, callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from app.models import User
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Привяжите аккаунт", show_alert=True)
            return
        
        repo = ManagerActionRepository(session, lead.tenant_id)
        await repo.log_action(
            lead_id=lead_id,
            user_id=user.id,
            action_type="first_contact",
            meta={"via": "telegram_bot"},
        )
        await session.commit()
    await callback.answer("Первый контакт зафиксирован!", show_alert=True)


@dp.callback_query(F.data.startswith("action_client_responded:"))
async def cb_client_responded(callback: CallbackQuery):
    """Manager records client response."""
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        if not await _check_lead_access(lead, callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from app.models import User
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Привяжите аккаунт", show_alert=True)
            return
        
        repo = ManagerActionRepository(session, lead.tenant_id)
        await repo.log_action(
            lead_id=lead_id,
            user_id=user.id,
            action_type="client_responded",
            meta={"via": "telegram_bot"},
        )
        await session.commit()
    await callback.answer("Ответ клиента записан!", show_alert=True)


@dp.callback_query(F.data.startswith("action_appointment:"))
async def cb_appointment(callback: CallbackQuery):
    """Manager records appointment set."""
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        if not await _check_lead_access(lead, callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from app.models import User
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Привяжите аккаунт", show_alert=True)
            return
        
        repo = ManagerActionRepository(session, lead.tenant_id)
        await repo.log_action(
            lead_id=lead_id,
            user_id=user.id,
            action_type="appointment_set",
            meta={"via": "telegram_bot"},
        )
        await session.commit()
    await callback.answer("Встреча назначена!", show_alert=True)


from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Мои лиды"), KeyboardButton(text="Моя статистика")],
        [KeyboardButton(text="Мой KPI"), KeyboardButton(text="🔍 Поиск")],
        [KeyboardButton(text="🔥 Топ"), KeyboardButton(text="Помощь")],
    ],
    resize_keyboard=True,
)

ADMIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Мои лиды"), KeyboardButton(text="Моя статистика")],
        [KeyboardButton(text="Мой KPI"), KeyboardButton(text="🔍 Поиск")],
        [KeyboardButton(text="🔥 Топ"), KeyboardButton(text="Привязать аккаунт")],
        [KeyboardButton(text="Помощь")],
    ],
    resize_keyboard=True,
)


@dp.message(F.text == "/start")
async def cmd_start(message):
    user = await _find_user(message)
    if user:
        kb = ADMIN_KB if user.role in ("super_admin", "admin") else MAIN_KB
        await message.answer(
            f"Привет, <b>{user.full_name or user.username}</b>!\n\n"
            f"Я Lead Hunter бот. Вот что я умею:\n\n"
            f"<b>Мои лиды</b> — ваши активные лиды\n"
            f"<b>Статистика</b> — за 7 дней\n"
            f"<b>KPI</b> — ваши цели и прогресс\n"
            f"<b>Привязать</b> — привязать Telegram\n\n"
            f"Или используйте команды:\n"
            f"/myleads — мои лиды\n"
            f"/mystats — статистика\n"
            f"/kpi — KPI\n"
            f"/link — привязка аккаунта",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await message.answer(
            "Добро пожаловать!\n\n"
            "Для начала работы привяжите аккаунт:\n"
            "/link <code>логин</code>",
            parse_mode="HTML",
            reply_markup=ADMIN_KB,
        )


@dp.message(F.text == "Мои лиды")
async def btn_my_leads(message):
    await cmd_my_leads(message)


@dp.message(F.text == "Моя статистика")
async def btn_my_stats(message):
    await cmd_my_stats(message)


@dp.message(F.text == "Мой KPI")
async def btn_kpi(message):
    await cmd_kpi(message)


@dp.message(F.text == "Привязать аккаунт")
async def btn_link(message):
    await cmd_link(message)


@dp.message(F.text == "Помощь")
async def btn_help(message):
    await message.answer(
        "<b>Команды бота:</b>\n\n"
        "/myleads — ваши активные лиды\n"
        "/mystats — статистика за 7 дней\n"
        "/kpi — ваши KPI и цели\n"
        "/top — топ лидов по Score\n"
        "/overdue — просроченные лиды\n"
        "/deals — последние сделки\n"
        "/search <code>запрос</code> — поиск по лидам\n"
        "/lead <code>ID</code> — информация по лиду\n"
        "/status <code>ID</code> <code>статус</code> — изменить статус\n"
        "/link — привязка аккаунта\n\n"
        "Статусы: new, contacted, interested, deal, not_interested",
        parse_mode="HTML",
    )


@dp.message(F.text.startswith("/myleads"))
async def cmd_my_leads(message):
    from app.models import User
    user = await _find_user(message)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Используйте /link для привязки аккаунта.")
        return

    async with async_session() as session:
        leads_result = await session.execute(
            select(Lead).where(
                Lead.assigned_to == user.id,
                Lead.status.notin_(["deleted", "archive"]),
            ).order_by(Lead.created_at.desc()).limit(10)
        )
        leads = leads_result.scalars().all()

    if not leads:
        await message.answer("📋 У вас нет активных лидов")
        return

    lines = ["📋 <b>Мои лиды (топ-10):</b>\n"]
    for lead in leads:
        icon = {"high": "🔥", "medium": "⚡", "low": "💤"}.get(lead.urgency, "")
        status_label = lead.status_label()
        name = lead.first_name or lead.username or f"#{lead.id}"
        lines.append(f"#{lead.id} {icon} <b>{name}</b> — {status_label} (score: {int(lead.lead_score)})")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text.startswith("/status"))
async def cmd_status(message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /status &lt;id&gt; &lt;статус&gt;\nСтатусы: new, contacted, interested, deal, not_interested")
        return

    try:
        lead_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Неверный ID лида")
        return

    new_status = parts[2].lower()
    valid_statuses = ["new", "contacted", "interested", "deal", "not_interested", "in_progress", "missed_call", "archive"]
    if new_status not in valid_statuses:
        await message.answer(f"❌ Неверный статус. Допустимые: {', '.join(valid_statuses)}")
        return

    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await message.answer("❌ Лид не найден")
            return

        from app.models import User
        user_result = await session.execute(
            select(User).where(
                (User.telegram_id == message.from_user.id) |
                (User.username == message.from_user.username)
            )
        )
        user = user_result.scalar_one_or_none()
        if not user or (lead.assigned_to != user.id and user.role not in ("super_admin", "admin")):
            await message.answer("❌ Нет доступа к этому лиду. Используйте /link")
            return

        old_status = lead.status
        lead.status = new_status
        await ActionLogRepository(session, lead.tenant_id).log(
            user.id, "status_change", lead_id=lead_id,
            meta={"from": old_status, "to": new_status, "via": "telegram_bot"},
        )
        await session.commit()

        # Train ML on outcome
        try:
            from app.ml_scorer import train_on_outcome
            if new_status == "deal":
                lead.feedback = "useful"
                train_on_outcome(lead, is_positive=True)
            elif new_status in ("not_interested", "deleted"):
                lead.feedback = "not_useful"
                train_on_outcome(lead, is_positive=False)
        except Exception:
            pass

    await message.answer(f"✅ Лид #{lead_id}: {old_status} → {new_status}")


@dp.message(F.text.startswith("/lead"))
async def cmd_lead(message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /lead &lt;id&gt;")
        return

    try:
        lead_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Неверный ID лида")
        return

    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await message.answer("❌ Лид не найден")
            return

        from app.models import User
        user_result = await session.execute(
            select(User).where(
                (User.telegram_id == message.from_user.id) |
                (User.username == message.from_user.username)
            )
        )
        user = user_result.scalar_one_or_none()
        if not user or (lead.assigned_to != user.id and user.role not in ("super_admin", "admin")):
            await message.answer("❌ Нет доступа к этому лиду. Используйте /link")
            return

    icon = {"high": "🔥", "medium": "⚡", "low": "💤"}.get(lead.urgency, "")
    text = (
        f"👤 <b>Лид #{lead.id}</b>\n\n"
        f"📝 {html.escape((lead.message_text or '')[:300])}\n\n"
        f"📊 Score: <b>{int(lead.lead_score)}</b> {icon}\n"
        f"📋 Статус: <b>{lead.status_label()}</b>\n"
        f"🏠 Чат: {html.escape(lead.chat_title or '')}\n"
        f"📅 Дата: {lead.created_at.strftime('%d.%m %H:%M') if lead.created_at else '?'}\n"
    )
    if lead.username:
        text += f"👤 @{html.escape(lead.username)}\n"
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text.startswith("/mystats"))
async def cmd_my_stats(message):
    from app.models import User
    from datetime import datetime, timedelta

    user = await _find_user(message)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Используйте /link для привязки аккаунта.")
        return

    since = utcnow() - timedelta(days=7)

    async with async_session() as session:
        total_q = select(Lead).where(
            Lead.assigned_to == user.id,
            Lead.status.notin_(["deleted", "archive"]),
        )
        total = len((await session.execute(total_q)).scalars().all())

        new_q = select(Lead).where(
            Lead.assigned_to == user.id, Lead.status == "new"
        )
        new_count = len((await session.execute(new_q)).scalars().all())

        deal_q = select(Lead).where(
            Lead.assigned_to == user.id, Lead.status == "deal"
        )
        deal_count = len((await session.execute(deal_q)).scalars().all())

        week_q = select(Lead).where(
            Lead.assigned_to == user.id,
            Lead.created_at >= since,
        )
        week_count = len((await session.execute(week_q)).scalars().all())

    text = (
        f"📊 <b>Ваша статистика (7 дней):</b>\n\n"
        f"📋 Активных лидов: <b>{total}</b>\n"
        f"🆕 Новых: <b>{new_count}</b>\n"
        f"🤝 Сделок: <b>{deal_count}</b>\n"
        f"📅 Получено за неделю: <b>{week_count}</b>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text.startswith("/link"))
async def cmd_link(message):
    from app.models import User
    _cleanup_pending_links()
    tg_id = message.from_user.id
    parts = message.text.split(maxsplit=1)
    target_username = parts[1].strip() if len(parts) > 1 else None

    # Check if user sent a verification code
    if target_username and target_username.isdigit() and len(target_username) == 6:
        pending = _pending_links.get(message.chat.id)
        if pending and pending["code"] == target_username and time.time() < pending["expires"]:
            async with async_session() as session:
                result = await session.execute(
                    select(User).where(User.username == pending["username"])
                )
                user = result.scalar_one_or_none()
                if user:
                    user.telegram_id = tg_id
                    await session.commit()
                    del _pending_links[message.chat.id]
                    await message.answer(
                        f"✅ Привязка выполнена!\n"
                        f"Пользователь: <b>{user.full_name or user.username}</b>",
                        parse_mode="HTML"
                    )
                    return
        if pending:
            del _pending_links[message.chat.id]
        await message.answer("❌ Неверный или просроченный код. Попробуйте /link заново.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            await message.answer(f"✅ Вы уже привязаны как <b>{existing.full_name or existing.username}</b>", parse_mode="HTML")
            return

        if target_username:
            result = await session.execute(
                select(User).where(User.username == target_username)
            )
            user = result.scalar_one_or_none()
            if not user:
                await message.answer(f"❌ Пользователь <code>{target_username}</code> не найден", parse_mode="HTML")
                return
            if user.telegram_id:
                await message.answer(f"⚠️ Пользователь <b>{user.full_name or user.username}</b> уже привязан к другому Telegram", parse_mode="HTML")
                return

            # Generate verification code
            code = f"{random.randint(100000, 999999)}"
            _pending_links[message.chat.id] = {
                "username": target_username,
                "code": code,
                "expires": time.time() + 300,  # 5 minutes
            }

            # Send code to owner
            if settings.owner_chat_id:
                try:
                    await bot.send_message(
                        settings.owner_chat_id,
                        f"🔑 <b>Код подтверждения привязки</b>\n\n"
                        f"Пользователь: <b>{user.full_name or user.username}</b>\n"
                        f"Telegram: @{message.from_user.username or message.from_user.id}\n"
                        f"Код: <code>{code}</code>\n\n"
                        f"Отправьте этот код пользователю для подтверждения.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            await message.answer(
                f"🔐 Код подтверждения отправлен администратору.\n"
                f"Попросите его передать вам 6-значный код.\n"
                f"Затем отправьте: /link <code>КОД</code>\n"
                f"(действует 5 минут)",
                parse_mode="HTML"
            )
        else:
            result = await session.execute(
                select(User).where(User.telegram_id == None, User.is_active == True)
            )
            unlinked = result.scalars().all()
            if not unlinked:
                await message.answer("❌ Все пользователи уже привязаны")
                return
            lines = ["🔗 <b>Для привязки отправьте:</b>\n", "/link <code>логин</code>\n"]
            lines.append("Доступные пользователи:")
            for u in unlinked:
                lines.append(f"  • <code>{u.username}</code> — {u.full_name or u.role}")
            await message.answer("\n".join(lines), parse_mode="HTML")


async def _find_user(message):
    """Find user by telegram_id first, then by username. Returns detached user or None."""
    from app.models import User
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            return user
        result = await session.execute(
            select(User).where(User.username == message.from_user.username)
        )
        return result.scalar_one_or_none()


@dp.message(F.text.startswith("/kpi"))
async def cmd_kpi(message):
    from app.models import User, Lead
    from app.repositories import EmployeeTargetRepository
    from datetime import datetime

    user = await _find_user(message)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Используйте /link для привязки аккаунта.")
        return

    async with async_session() as session:
        current_period = utcnow().strftime("%Y-%m")
        targets_repo = EmployeeTargetRepository(session, user.tenant_id)
        target = await targets_repo.get_or_create(user.id, current_period)

        # Get actuals
        from sqlalchemy import func
        period_start = datetime.strptime(f"{current_period}-01", "%Y-%m-%d")
        if utcnow().month == 12:
            period_end = datetime.strptime(f"{utcnow().year + 1}-01-01", "%Y-%m-%d")
        else:
            period_end = datetime.strptime(f"{utcnow().year}-{utcnow().month + 1:02d}-01", "%Y-%m-%d")

        actual_leads = (await session.execute(
            select(func.count(Lead.id)).where(
                Lead.assigned_to == user.id,
                Lead.created_at >= period_start,
                Lead.created_at < period_end,
            )
        )).scalar() or 0

        actual_deals = (await session.execute(
            select(func.count(Lead.id)).where(
                Lead.assigned_to == user.id,
                Lead.status == "deal",
                Lead.created_at >= period_start,
                Lead.created_at < period_end,
            )
        )).scalar() or 0

        revenue = (await session.execute(
            select(func.sum(Lead.deal_amount)).where(
                Lead.assigned_to == user.id,
                Lead.deal_amount.isnot(None),
                Lead.created_at >= period_start,
                Lead.created_at < period_end,
            )
        )).scalar() or 0

        await targets_repo.update_actuals(user.id, current_period, actual_leads, actual_deals, revenue)
        await session.commit()

    def pct(actual, target):
        return f"{min(100, round(actual / target * 100))}%" if target > 0 else "—"

    def bar(actual, target, size=10):
        if target <= 0:
            return "░" * size
        filled = min(size, round(actual / target * size))
        return "█" * filled + "░" * (size - filled)

    text = (
        f"🎯 <b>KPI — {current_period}</b>\n\n"
        f"📋 <b>Лиды:</b> {actual_leads}/{target.target_leads} {pct(actual_leads, target.target_leads)}\n"
        f"   {bar(actual_leads, target.target_leads)}\n\n"
        f"🤝 <b>Сделки:</b> {actual_deals}/{target.target_deals} {pct(actual_deals, target.target_deals)}\n"
        f"   {bar(actual_deals, target.target_deals)}\n\n"
        f"💰 <b>Выручка:</b> {int(revenue):,}/{int(target.target_revenue):,} ₽ {pct(revenue, target.target_revenue)}\n"
        f"   {bar(revenue, target.target_revenue)}\n\n"
        f"📊 Выполнение: <b>{pct((actual_leads + actual_deals + revenue/10000) / max(1, target.target_leads + target.target_deals + target.target_revenue/10000) * 100, 100)}</b>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "🔍 Поиск")
async def btn_search(message):
    await message.answer("Введите поисковый запрос:\n/search <текст>", parse_mode="HTML")


@dp.message(F.text == "🔥 Топ")
async def btn_top(message):
    await cmd_top(message)


@dp.message(F.text.startswith("/search"))
async def cmd_search(message):
    user = await _find_user(message)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Используйте /link для привязки аккаунта.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("🔍 Введите поисковый запрос:\n/search <текст>")
        return

    query = f"%{parts[1].strip()}%"

    async with async_session() as session:
        from sqlalchemy import or_
        leads_result = await session.execute(
            select(Lead).where(
                Lead.tenant_id == user.tenant_id,
                Lead.status.notin_(["deleted", "archive"]),
                or_(
                    Lead.first_name.ilike(query),
                    Lead.last_name.ilike(query),
                    Lead.username.ilike(query),
                    Lead.message_text.ilike(query),
                ),
            ).order_by(Lead.lead_score.desc()).limit(10)
        )
        leads = leads_result.scalars().all()

    if not leads:
        await message.answer("🔍 Ничего не найдено")
        return

    lines = [f"🔍 <b>Результаты поиска ({len(leads)}):</b>\n"]
    for lead in leads:
        icon = {"high": "🔥", "medium": "⚡", "low": "💤"}.get(lead.urgency, "")
        name = html.escape(lead.first_name or lead.username or f"#{lead.id}")
        lines.append(
            f"#{lead.id} {icon} <b>{name}</b> — {lead.status_label()} (score: {int(lead.lead_score)})"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text == "/top")
async def cmd_top(message):
    user = await _find_user(message)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Используйте /link для привязки аккаунта.")
        return

    async with async_session() as session:
        leads_result = await session.execute(
            select(Lead).where(
                Lead.tenant_id == user.tenant_id,
                Lead.status.notin_(["deleted", "archive"]),
            ).order_by(Lead.lead_score.desc()).limit(10)
        )
        leads = leads_result.scalars().all()

    if not leads:
        await message.answer("📋 Нет лидов для отображения")
        return

    lines = ["🔥 <b>Топ-10 лидов по Score:</b>\n"]
    for i, lead in enumerate(leads, 1):
        icon = {"high": "🔥", "medium": "⚡", "low": "💤"}.get(lead.urgency, "")
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        name = html.escape(lead.first_name or lead.username or f"#{lead.id}")
        lines.append(
            f"{medal} #{lead.id} {icon} <b>{name}</b> — {lead.status_label()} (score: {int(lead.lead_score)})"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text == "/overdue")
async def cmd_overdue(message):
    user = await _find_user(message)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Используйте /link для привязки аккаунта.")
        return

    from datetime import timedelta
    cutoff = utcnow() - timedelta(hours=4)

    async with async_session() as session:
        leads_result = await session.execute(
            select(Lead).where(
                Lead.tenant_id == user.tenant_id,
                Lead.status == "new",
                Lead.created_at < cutoff,
            ).order_by(Lead.created_at.asc()).limit(5)
        )
        leads = leads_result.scalars().all()

        total_result = await session.execute(
            select(Lead).where(
                Lead.tenant_id == user.tenant_id,
                Lead.status == "new",
                Lead.created_at < cutoff,
            )
        )
        total = len(total_result.scalars().all())

    if total == 0:
        await message.answer("✅ Просроченных лидов нет!")
        return

    now = utcnow()
    lines = [f"⏰ <b>Просроченные лиды ({total}):</b>\n"]
    for lead in leads:
        age = now - (lead.created_at.replace(tzinfo=None) if lead.created_at else now)
        hours = int(age.total_seconds() / 3600)
        icon = {"high": "🔥", "medium": "⚡", "low": "💤"}.get(lead.urgency, "")
        name = html.escape(lead.first_name or lead.username or f"#{lead.id}")
        lines.append(
            f"#{lead.id} {icon} <b>{name}</b> — {hours}ч ожидания (score: {int(lead.lead_score)})"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text == "/deals")
async def cmd_deals(message):
    user = await _find_user(message)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Используйте /link для привязки аккаунта.")
        return

    async with async_session() as session:
        leads_result = await session.execute(
            select(Lead).where(
                Lead.tenant_id == user.tenant_id,
                Lead.status == "deal",
            ).order_by(Lead.deal_closed_at.desc().nullslast()).limit(10)
        )
        leads = leads_result.scalars().all()

    if not leads:
        await message.answer("🤝 Нет сделок для отображения")
        return

    lines = ["🤝 <b>Последние сделки:</b>\n"]
    for lead in leads:
        name = html.escape(lead.first_name or lead.username or f"#{lead.id}")
        amount = f"{int(lead.deal_amount):,} ₽" if lead.deal_amount else "—"
        date = lead.deal_closed_at.strftime("%d.%m %H:%M") if lead.deal_closed_at else "—"
        lines.append(f"#{lead.id} <b>{name}</b> — {amount} ({date})")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def start_bot():
    await dp.start_polling(bot)


async def send_anomaly_notification(owner_chat_id: int, message: str):
    """Send anomaly alert to the owner via Telegram."""
    try:
        await bot.send_message(owner_chat_id, message, parse_mode="HTML")
    except Exception as e:
        logger.error("Failed to send anomaly notification to %d: %s", owner_chat_id, e)


async def check_and_notify_anomalies():
    """Check all tenants for anomalies and send notifications to owners."""
    from datetime import datetime, timedelta
    from app.models import Tenant, ActionLog

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.is_active == True))
        tenants = result.scalars().all()

    for tenant in tenants:
        try:
            tenant_config = await TenantConfig.create(tenant.id)
            owner_chat_id = tenant_config.owner_chat_id
            if not owner_chat_id:
                continue

            sla_hours = tenant.config.get("sla_hours", 4) if tenant.config else 4
            since = utcnow() - timedelta(hours=sla_hours * 3)

            async with async_session() as session:
                action_log = ActionLogRepository(session, tenant.id)

                # 1. Existing SLA anomaly check
                anomalies = await action_log.get_write_then_no_status_change(since, hours=sla_hours)

                if anomalies:
                    user_ids = list({a.user_id for a in anomalies if a.user_id})
                    user_map = {}
                    if user_ids:
                        from app.models import User
                        result = await session.execute(
                            select(User).where(User.id.in_(user_ids))
                        )
                        user_map = {u.id: u for u in result.scalars().all()}

                    for a in anomalies[:5]:
                        user = user_map.get(a.user_id) if a.user_id else None
                        name = user.full_name or user.username if user else "Неизвестный"
                        time_str = a.created_at.strftime("%d.%m %H:%M")
                        text = (
                            f"⚠️ <b>Аномалия SLA</b>\n\n"
                            f"Сотрудник <b>{name}</b> нажал «Написать» по лиду "
                            f"<a href=\"{settings.get_site_url()}/leads/{a.lead_id}\">#{a.lead_id}</a> "
                            f"({time_str}), статус не менялся {sla_hours}+ ч."
                        )
                        await send_anomaly_notification(owner_chat_id, text)

                # 2. Mass deletion detection (10+ deletes in 1 hour)
                hour_ago = utcnow() - timedelta(hours=1)
                mass_delete_q = (
                    select(ActionLog.user_id, func.count(ActionLog.id).label("cnt"))
                    .where(
                        ActionLog.tenant_id == tenant.id,
                        ActionLog.action_type == "status_change",
                        ActionLog.created_at >= hour_ago,
                    )
                    .group_by(ActionLog.user_id)
                    .having(func.count(ActionLog.id) >= 10)
                )
                mass_deletes = (await session.execute(mass_delete_q)).fetchall()

                for uid, cnt in mass_deletes:
                    if uid:
                        from app.models import User
                        user = await session.get(User, uid)
                        name = user.full_name or user.username if user else f"ID:{uid}"

                        # Check how many were deletions
                        delete_q = (
                            select(func.count(ActionLog.id))
                            .where(
                                ActionLog.tenant_id == tenant.id,
                                ActionLog.user_id == uid,
                                ActionLog.action_type == "status_change",
                                ActionLog.created_at >= hour_ago,
                            )
                        )
                        total_changes = (await session.execute(delete_q)).scalar() or 0

                        if total_changes >= 10:
                            text = (
                                f"🚨 <b>Массовые изменения!</b>\n\n"
                                f"Сотрудник <b>{name}</b> сменил статус <b>{total_changes} раз</b> за час.\n"
                                f"⚠️ Возможно скрытие или удаление лидов."
                            )
                            await send_anomaly_notification(owner_chat_id, text)

                # 3. CSV export tracking
                export_q = (
                    select(ActionLog.user_id, func.count(ActionLog.id).label("cnt"))
                    .where(
                        ActionLog.tenant_id == tenant.id,
                        ActionLog.action_type == "csv_export",
                        ActionLog.created_at >= hour_ago,
                    )
                    .group_by(ActionLog.user_id)
                    .having(func.count(ActionLog.id) >= 3)
                )
                exports = (await session.execute(export_q)).fetchall()

                for uid, cnt in exports:
                    if uid:
                        from app.models import User
                        user = await session.get(User, uid)
                        name = user.full_name or user.username if user else f"ID:{uid}"
                        text = (
                            f"⚠️ <b>Массовый экспорт CSV</b>\n\n"
                            f"Сотрудник <b>{name}</b> скачал CSV <b>{cnt} раз</b> за час.\n"
                            f"⚠️ Возможна утечка клиентской базы."
                        )
                        await send_anomaly_notification(owner_chat_id, text)

                # 4. Profile clicks without status change (data theft indicator)
                profile_q = (
                    select(ActionLog.user_id, func.count(ActionLog.id).label("cnt"))
                    .where(
                        ActionLog.tenant_id == tenant.id,
                        ActionLog.action_type == "profile_click",
                        ActionLog.created_at >= hour_ago,
                    )
                    .group_by(ActionLog.user_id)
                    .having(func.count(ActionLog.id) >= 5)
                )
                profiles = (await session.execute(profile_q)).fetchall()

                for uid, cnt in profiles:
                    if uid:
                        from app.models import User
                        user = await session.get(User, uid)
                        name = user.full_name or user.username if user else f"ID:{uid}"
                        text = (
                            f"⚠️ <b>Массовые переходы по профилям</b>\n\n"
                            f"Сотрудник <b>{name}</b> перешёл на профили <b>{cnt} раз</b> за час.\n"
                            f"⚠️ Возможно копирование контактов."
                        )
                        await send_anomaly_notification(owner_chat_id, text)

        except Exception as e:
            logger.error("Anomaly check failed for tenant %d: %s", tenant.id, e)


async def send_reminder_notification(chat_id: int, message: str):
    try:
        import asyncio
        await asyncio.sleep(1.5)
        await bot.send_message(chat_id, message, parse_mode="HTML")
    except Exception as e:
        logger.error("Failed to send reminder to %d: %s", chat_id, e)


async def check_and_send_reminders():
    """Check all tenants for unprocessed leads and send reminders."""
    from datetime import datetime, timedelta
    from app.models import Tenant, User, Lead
    from app.repositories import UserRepository
    from sqlalchemy import select, func
    import asyncio

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.is_active == True))
        tenants = result.scalars().all()

    MAX_REMINDERS = 3
    MAX_ESCALATIONS = 2

    for tenant in tenants:
        try:
            tc = await _get_tenant_config(tenant.id)
            reminder_hours = tc._config.get("reminder_hours", 2)
            escalation_hours = tc._config.get("escalation_hours", 4)
            owner_chat_id = tc.owner_chat_id
            if not owner_chat_id:
                continue

            cutoff_reminder = utcnow() - timedelta(hours=reminder_hours)
            cutoff_escalation = utcnow() - timedelta(hours=escalation_hours)

            async with async_session() as session:
                users_repo = UserRepository(session, tenant.id)
                managers = await users_repo.list_active()
                manager_map = {m.id: m for m in managers if m.role in ("admin", "manager")}

                # Reminder leads: new, unassigned or assigned, older than reminder_hours
                q = select(Lead).where(
                    Lead.tenant_id == tenant.id,
                    Lead.status == "new",
                    Lead.created_at <= cutoff_reminder,
                )
                leads = (await session.execute(q)).scalars().all()

                reminder_count = 0
                escalation_count = 0

                for lead in leads:
                    age_hours = (utcnow() - lead.created_at).total_seconds() / 3600

                    if age_hours >= escalation_hours and lead.assigned_to:
                        if escalation_count >= MAX_ESCALATIONS:
                            continue
                        escalation_count += 1
                        assignee = manager_map.get(lead.assigned_to)
                        assignee_name = assignee.full_name or assignee.username if assignee else "Неизвестный"
                        text = (
                            f"🚨 <b>ЭСКАЛАЦИЯ</b>\n\n"
                            f"Лид <a href=\"{settings.get_site_url()}/leads/{lead.id}\">#{lead.id}</a> "
                            f"не обработан {age_hours:.0f} ч.\n"
                            f"Назначен: {assignee_name}\n"
                            f"Score: {int(lead.lead_score)} | {lead.urgency}\n"
                            f"Чат: {lead.chat_title[:40]}"
                        )
                        await send_reminder_notification(owner_chat_id, text)
                    elif age_hours >= reminder_hours:
                        if reminder_count >= MAX_REMINDERS:
                            continue
                        reminder_count += 1
                        target_chat = None
                        if lead.assigned_to:
                            assignee = manager_map.get(lead.assigned_to)
                            if assignee and assignee.telegram_id:
                                target_chat = assignee.telegram_id
                        if not target_chat:
                            target_chat = owner_chat_id

                        text = (
                            f"⏰ <b>Напоминание</b>\n\n"
                            f"Лид <a href=\"{settings.get_site_url()}/leads/{lead.id}\">#{lead.id}</a> "
                            f"ожидает обработки {age_hours:.0f} ч.\n"
                            f"Score: {int(lead.lead_score)} | {lead.urgency}\n"
                            f"Чат: {lead.chat_title[:40]}"
                        )
                        await send_reminder_notification(target_chat, text)

                # Follow-up reminders: contacted/in_progress leads without response
                followup_q = select(Lead).where(
                    Lead.tenant_id == tenant.id,
                    Lead.status.in_(["contacted", "in_progress"]),
                    Lead.assigned_to.isnot(None),
                    Lead.last_responded_at.isnot(None),
                    Lead.last_responded_at <= utcnow() - timedelta(hours=24),
                )
                followup_leads = (await session.execute(followup_q)).scalars().all()

                for lead in followup_leads[:3]:
                    assignee = manager_map.get(lead.assigned_to)
                    if not assignee or not assignee.telegram_id:
                        continue
                    hours_since = (utcnow() - lead.last_responded_at).total_seconds() / 3600
                    text = (
                        f"🔄 <b>Follow-up напоминание</b>\n\n"
                        f"Лид <a href=\"{settings.get_site_url()}/leads/{lead.id}\">#{lead.id}</a> "
                        f"без ответа {hours_since:.0f} ч.\n"
                        f"Score: {int(lead.lead_score)} | {lead.chat_title[:40]}\n\n"
                        f"Напишите клиенту или запланируйте повторный звонок."
                    )
                    await send_reminder_notification(assignee.telegram_id, text)

            logger.info("Reminders check completed for tenant %d", tenant.id)
        except Exception as e:
            logger.error("Reminder check failed for tenant %d: %s", tenant.id, e)


async def auto_assign_leads():
    """Auto-assign unassigned new leads to the least-loaded manager."""
    from app.models import Tenant, Lead, User
    from app.repositories import UserRepository
    from sqlalchemy import select, func

    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.is_active == True))
        tenants = result.scalars().all()

    for tenant in tenants:
        try:
            tc = await _get_tenant_config(tenant.id)
            if not tc._config.get("auto_assign", False):
                continue

            async with async_session() as session:
                users_repo = UserRepository(session, tenant.id)
                managers = await users_repo.list_active()
                active_managers = [m for m in managers if m.role in ("admin", "manager")]
                if not active_managers:
                    continue

                # Get unassigned leads
                q = select(Lead).where(
                    Lead.tenant_id == tenant.id,
                    Lead.status == "new",
                    Lead.assigned_to.is_(None),
                )
                unassigned = (await session.execute(q)).scalars().all()

                if not unassigned:
                    continue

                # Count active leads per manager
                load_q = (
                    select(Lead.assigned_to, func.count(Lead.id))
                    .where(
                        Lead.tenant_id == tenant.id,
                        Lead.status.in_(["new", "contacted", "in_progress"]),
                        Lead.assigned_to.isnot(None),
                    )
                    .group_by(Lead.assigned_to)
                )
                load_rows = (await session.execute(load_q)).fetchall()
                load_map = {uid: cnt for uid, cnt in load_rows}

                # Weighted assignment with max_leads limit
                for lead in unassigned:
                    # Filter managers by max_leads limit
                    eligible = [
                        m for m in active_managers
                        if load_map.get(m.id, 0) < (m.max_leads or 50)
                    ]
                    if not eligible:
                        eligible = active_managers  # fallback to all

                    # Weighted selection: lower load / higher weight = better
                    def weighted_score(m):
                        current_load = load_map.get(m.id, 0)
                        weight = m.weight or 1.0
                        return current_load / weight

                    best_manager = min(eligible, key=weighted_score)
                    lead.assigned_to = best_manager.id
                    load_map[best_manager.id] = load_map.get(best_manager.id, 0) + 1

                    from app.repositories import LeadHistoryRepository, ActionLogRepository, ResponseTimeRepository
                    history = LeadHistoryRepository(session, tenant.id)
                    await history.create(
                        lead_id=lead.id,
                        user_id=None,
                        action="auto_assigned",
                        note=f"Авто-назначен: {best_manager.full_name or best_manager.username}",
                    )
                    await ActionLogRepository(session, tenant.id).log(
                        None, "auto_assign", lead_id=lead.id,
                        meta={"assigned_to": best_manager.id, "via": "auto_assign"},
                    )
                    # Record response time tracking
                    resp_repo = ResponseTimeRepository(session, tenant.id)
                    await resp_repo.record_assignment(best_manager.id, lead.id)

                await session.commit()
                if unassigned:
                    logger.info("Auto-assigned %d leads for tenant %d", len(unassigned), tenant.id)

        except Exception as e:
            logger.error("Auto-assign failed for tenant %d: %s", tenant.id, e)
