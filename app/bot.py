import html
import json
import logging
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from app.models import Lead, Tenant, async_session
from app.config_manager import TenantConfig
from app.repositories import ActionLogRepository
from config import settings

logger = logging.getLogger(__name__)

bot = Bot(token=settings.bot_token)
dp = Dispatcher()


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

    urgency_icon = {"high": "🔥", "medium": "⚡", "low": "💤"}.get(lead.urgency, "")
    text = (
        f"🔥 <b>Новый лид</b>\n\n"
        f"🏠 Чат: <b>{chat_title}</b>\n"
        f"👤 Пользователь: <b>{first_name} {last_name}</b>\n"
        f"   @{username}\n\n"
    )
    if lead.reply_to_text:
        text += f"↩️ Ответ на:\n<i>{reply_to_text}</i>\n\n"
    text += (
        f"💬 Сообщение:\n<i>{message_text}</i>\n\n"
        f"📊 Lead Score: <b>{lead.lead_score}</b> {urgency_icon}\n"
        f"⏰ Срочность: <b>{lead.urgency}</b>\n\n"
        f"📝 Причина:\n{reason}\n\n"
    )
    return text


def _lead_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    buttons = []

    profile = lead.profile_url()
    if profile and profile != "#":
        buttons.append(
            [
                InlineKeyboardButton(
                    text="👤 Открыть профиль",
                    url=profile,
                ),
            ],
        )

    if lead.chat_username and lead.message_id:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="💬 Открыть сообщение",
                    url=f"https://t.me/{lead.chat_username}/{lead.message_id}",
                ),
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="📦 Архив",
                callback_data=f"archive:{lead.id}",
            ),
            InlineKeyboardButton(
                text="❌ Не лид",
                callback_data=f"not_lead:{lead.id}",
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="✅ Контактировали",
                callback_data=f"contacted:{lead.id}",
            ),
            InlineKeyboardButton(
                text="🤝 Сделка",
                callback_data=f"deal:{lead.id}",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_lead_notification(lead: Lead):
    try:
        tenant_config = await _get_tenant_config(lead.tenant_id)
        owner_chat_id = tenant_config.owner_chat_id

        text = _lead_text(lead)
        kb = _lead_keyboard(lead)
        try:
            await bot.send_message(
                chat_id=owner_chat_id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            # Defense in depth: even with escaping in _lead_text, don't let a
            # formatting edge case silently swallow the notification entirely.
            logger.exception("HTML notification failed for lead %d — retrying as plain text", lead.id)
            plain_text = re.sub(r"<[^>]+>", "", text)
            await bot.send_message(chat_id=owner_chat_id, text=plain_text, reply_markup=kb)
        async with async_session() as session:
            await session.execute(
                update(Lead).where(Lead.id == lead.id).values(is_notified=1)
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to send notification")


@dp.callback_query(F.data.startswith("archive:"))
async def cb_archive(callback: CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        tenant_config = await _get_tenant_config(lead.tenant_id)
        if callback.from_user.id != tenant_config.owner_chat_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await session.execute(
            update(Lead).where(Lead.id == lead_id).values(status="archive")
        )
        await ActionLogRepository(session, lead.tenant_id).log(
            callback.from_user.id, "status_change", lead_id=lead_id,
            meta={"from": lead.status, "to": "archive"},
        )
        await session.commit()
    await callback.answer("📦 Архивировано")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(callback.message.text + "\n\n📦 <b>Архивировано</b>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("not_lead:"))
async def cb_not_lead(callback: CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        tenant_config = await _get_tenant_config(lead.tenant_id)
        if callback.from_user.id != tenant_config.owner_chat_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await session.execute(
            update(Lead).where(Lead.id == lead_id).values(status="not_interested")
        )
        await ActionLogRepository(session, lead.tenant_id).log(
            callback.from_user.id, "status_change", lead_id=lead_id,
            meta={"from": lead.status, "to": "not_interested"},
        )
        await session.commit()
    await callback.answer("❌ Не лид")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Не лид</b>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("contacted:"))
async def cb_contacted(callback: CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        tenant_config = await _get_tenant_config(lead.tenant_id)
        if callback.from_user.id != tenant_config.owner_chat_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await session.execute(
            update(Lead).where(Lead.id == lead_id).values(status="contacted")
        )
        await ActionLogRepository(session, lead.tenant_id).log(
            callback.from_user.id, "status_change", lead_id=lead_id,
            meta={"from": lead.status, "to": "contacted"},
        )
        await session.commit()
    await callback.answer("✅ Контактировали")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Контактировали</b>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("deal:"))
async def cb_deal(callback: CallbackQuery):
    lead_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            await callback.answer("Лид не найден", show_alert=True)
            return
        tenant_config = await _get_tenant_config(lead.tenant_id)
        if callback.from_user.id != tenant_config.owner_chat_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await session.execute(
            update(Lead).where(Lead.id == lead_id).values(status="deal")
        )
        await ActionLogRepository(session, lead.tenant_id).log(
            callback.from_user.id, "status_change", lead_id=lead_id,
            meta={"from": lead.status, "to": "deal"},
        )
        await session.commit()
    await callback.answer("🤝 Сделка!")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text(callback.message.text + "\n\n🤝 <b>Сделка!</b>", parse_mode="HTML")


async def start_bot():
    await dp.start_polling(bot)
