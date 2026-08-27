import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat
from sqlalchemy import select, and_, func, delete
from app.models import Lead, User, LeadHistory, BlacklistedUser, ProcessedMessage, Tenant, async_session
from app.analyzer import analyze_message
from app.bot import send_lead_notification
from app.config_manager import TenantConfig
from config import settings, AD_PATTERNS, AD_PHRASES, utcnow

logger = logging.getLogger(__name__)

# Global client is removed — use TelegramClientFactory instead

_user_context: dict[int, list[str]] = {}
MAX_CONTEXT = 10
MAX_CONTEXT_USERS = 500
HISTORY_MONTHS = 2
_CLEANUP_DAYS = 30
_SCAN_SEMAPHORE = asyncio.Semaphore(1)

NOISE_THRESHOLD = 3

SCAN_STATE_FILE = Path("scan_state.json")


async def _save_user_message(tenant_id: int, user_id: int, username: str, first_name: str,
                              chat_title: str, message_id: int, text: str):
    """Save user message to history for context analysis."""
    if not user_id:
        return
    try:
        async with async_session() as session:
            from app.models import UserMessageHistory
            msg = UserMessageHistory(
                tenant_id=tenant_id,
                user_id=user_id,
                username=username,
                first_name=first_name,
                chat_title=chat_title,
                message_id=message_id,
                text=text[:2000],
            )
            session.add(msg)
            await session.commit()
    except Exception:
        pass


async def _get_user_history(tenant_id: int, user_id: int, chat_title: str, limit: int = 10) -> list[str]:
    """Get last N messages from user in this chat for context analysis."""
    if not user_id:
        return []
    try:
        async with async_session() as session:
            from app.models import UserMessageHistory
            from sqlalchemy import select as sel, desc
            result = await session.execute(
                sel(UserMessageHistory.text)
                .where(
                    UserMessageHistory.tenant_id == tenant_id,
                    UserMessageHistory.user_id == user_id,
                    UserMessageHistory.chat_title == chat_title,
                )
                .order_by(desc(UserMessageHistory.created_at))
                .limit(limit)
            )
            messages = [row[0] for row in result.all()]
            messages.reverse()
            return messages
    except Exception:
        return []


def _load_scan_state() -> dict:
    if SCAN_STATE_FILE.exists():
        try:
            return json.loads(SCAN_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_scan_state(state: dict):
    try:
        tmp = SCAN_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(SCAN_STATE_FILE)  # atomic — no torn writes on concurrent scans
    except Exception:
        pass


async def _transcribe_voice(message, tenant_id: int = None) -> Optional[str]:
    """Download and transcribe a voice message via OpenAI Whisper API.
    
    Checks billing limits before calling and logs usage after.
    Estimated cost: $0.006/minute (Whisper API pricing).
    """
    if not settings.openai_api_key:
        return None
    
    # Check billing limits before calling
    if tenant_id is not None:
        from app.models import async_session, TenantUsage
        from app.repositories import TenantUsageRepository
        from app.config_manager import TenantConfig

        try:
            async with async_session() as session:
                usage = TenantUsageRepository(session, tenant_id)
                tenant_config = await TenantConfig.create(tenant_id)

                # Check daily cost limit
                cost_today = await usage.sum_cost_today(tenant_id)
                if cost_today >= tenant_config.max_cost_per_month_usd / 30:  # daily share
                    logger.warning("Whisper daily cost limit reached for tenant %d ($%.4f)", tenant_id, cost_today)
                    return None
        except Exception:
            logger.exception("Whisper billing check failed for tenant %d — skipping transcription", tenant_id)
            return None
    
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        
        await message.download_media(file=tmp_path)
        
        # Estimate duration from file size (rough: OGG ~16KB/s for voice)
        file_size = os.path.getsize(tmp_path)
        estimated_seconds = file_size / 16000  # rough estimate
        estimated_minutes = estimated_seconds / 60
        estimated_cost = estimated_minutes * 0.006  # $0.006/min
        
        import aiohttp
        if "groq" in settings.openai_base_url:
            transcribe_url = "https://api.groq.com/openai/v1/audio/transcriptions"
            transcribe_model = "whisper-large-v3-turbo"
        else:
            transcribe_url = settings.openai_base_url.rstrip("/") + "/audio/transcriptions"
            transcribe_model = "whisper-1"
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
            data = aiohttp.FormData()
            data.add_field("file", open(tmp_path, "rb"), filename="voice.ogg", content_type="audio/ogg")
            data.add_field("model", transcribe_model)
            data.add_field("language", "ru")
            data.add_field("response_format", "text")

            async with session.post(transcribe_url,
                                     headers=headers, data=data) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    logger.info("Voice transcription: %s", text[:100])
                    
                    # Log usage
                    if tenant_id is not None:
                        async with async_session() as sess:
                            usage = TenantUsageRepository(sess, tenant_id)
                            await usage.log_event(
                                tenant_id=tenant_id,
                                event_type="whisper_transcription",
                                tokens_used=0,
                                model_used=transcribe_model,
                                cost_usd=estimated_cost,
                            )
                            await sess.commit()
                    
                    return text.strip()
                else:
                    logger.warning("Whisper API error: %d", resp.status)
                    return None
    except Exception as e:
        logger.warning("Voice transcription failed: %s", e)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _matches_keywords(text: str, keywords: set[str] = None) -> bool:
    if not keywords:
        return False
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _is_noise(text: str, noise_keywords: set[str] = None) -> bool:
    if not noise_keywords:
        return False
    lower = text.lower()
    noise_count = sum(1 for kw in noise_keywords if kw in lower)
    if noise_count >= NOISE_THRESHOLD:
        return True
    if len(lower) < 20 and noise_count >= 2:
        return True
    return False


def _is_ad(text: str) -> bool:
    """Detect contractor/self-promotion ads — not leads, but competitors."""
    lower = text.lower()

    # First-person plural about being a team/crew = contractor ad
    if re.search(r'(мы\s+(бригада|команда|коллектив|специалисты|мастера))', lower):
        return True
    if re.search(r'(наша\s+(команда|бригада|компания|фирма))', lower):
        return True
    if re.search(r'(работаем\s+(по|на|уже)\s+\d+)', lower):
        return True
    if re.search(r'(опыт\s+\d+\s+лет)', lower):
        return True
    if re.search(r'(скидк\w*\s+\d+%)', lower):
        return True
    if re.search(r'(звоните|пишите|свяжитесь)\s*,?\s*(бригад|мастер|специалист)', lower):
        return True

    # === Anti-master: contractor ads disguised as client requests ===
    # "Я занимаюсь ремонтом" — first person about offering services
    if re.search(r'я\s+(занимаюсь|делаю|выполняю|оказываю)\s+(ремонт|отделк|работ)', lower):
        return True
    # "Ремонт квартир под ключ с гарантией" — ad-like phrasing
    if re.search(r'(ремонт|отделк)\s+(квартир|комнат|дом)\s+под\s+ключ\s+(с\s+гаранти|гаранти)', lower):
        return True
    # "Если кому-то нужно — напишите" — soliciting, not requesting
    if re.search(r'(если\s+кому[-\s]?то\s+(нужн|надо|интересн))', lower):
        return True
    # "Мастер по ремонту" — self-identification as contractor.
    # But NOT when preceded by request-language ("нужен мастер", "ищу мастера",
    # "посоветуйте мастера") — that's a client looking for one, i.e. a real lead.
    _master_match = re.search(r'мастер\w*\s+(по\s+)?(ремонт|отделк|работ)', lower)
    if _master_match:
        prefix = lower[max(0, _master_match.start() - 40):_master_match.start()]
        _request_markers = (
            'нужен', 'нужна', 'нужны', 'ищу', 'ищем', 'подскажите', 'посоветуйте',
            'посоветовать', 'порекомендуйте', 'порекомендовать', 'посоветуете',
            'кто может', 'кто знает', 'кто-то', 'есть у кого', 'знает кто',
        )
        if not any(m in prefix for m in _request_markers):
            return True
    # "Пишите в личку/ЛС/лс" — soliciting
    if re.search(r'(пишите|напишите)\s+(в\s+)?(лс|личк|личн\w*\s+сообщен)', lower):
        return True
    # Phone + service offer
    if re.search(r'(тел\.?|телефон|звоните|связаться)\s*[.:]?\s*\+?\d[\d\s\-()]{7,}', lower):
        return True
    # "Работаем по договору" — contractor language
    if re.search(r'(работаем|выполняем)\s+по\s+(договор|смет)', lower):
        return True
    # "Выезд на замер бесплатно" — classic ad
    if re.search(r'(выезд|визит)\s+(на\s+)?замер\s+(бесплатн|free)', lower):
        return True
    # Portfolio/works showcase
    if re.search(r'(наше|моё|мои|наши)\s+(портфолио|работ\w*|пример\w*)', lower):
        return True
    # "Кто ищет.master" style
    if re.search(r'(кто\s+(ищет|ищет|нуждается)\s+(мастер|бригад|специалист))', lower):
        return True

    ad_score = 0
    reasons = []

    phone_count = 0
    for pattern in AD_PATTERNS[:2]:
        if re.search(pattern, text):
            phone_count += 1
    if phone_count >= 2:
        ad_score += 3
        reasons.append("phones")

    for pattern in AD_PATTERNS[2:5]:
        if re.search(pattern, text):
            ad_score += 1
            reasons.append("contact_info")

    for phrase in AD_PHRASES:
        if phrase in lower:
            ad_score += 2
            reasons.append(phrase)
    
    if text.isupper() and len(text) > 20:
        ad_score += 2
        reasons.append("all_caps")
    
    lines = text.split('\n')
    if len(lines) > 5:
        has_prices = any(re.search(r'\d+\s*(руб|₽|цена|стоимость)', line.lower()) for line in lines)
        if has_prices:
            ad_score += 2
            reasons.append("price_list")
    
    if ad_score >= 3:
        logger.debug("Ad detected (score=%d, reasons=%s): %s", ad_score, reasons, text[:100])
        return True
    
    return False


async def _is_duplicate(user_id: int, tenant_id: int = None) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.dedup_days)
    async with async_session() as session:
        bl = (await session.execute(
            select(BlacklistedUser).where(BlacklistedUser.user_id == user_id)
        )).scalar_one_or_none()
        if bl:
            return True

        cond = and_(Lead.user_id == user_id, Lead.created_at >= cutoff)
        if tenant_id:
            cond = and_(cond, Lead.tenant_id == tenant_id)
        result = await session.execute(
            select(Lead).where(cond)
        )
        return result.scalar_one_or_none() is not None


async def _is_msg_processed(chat_title: str, message_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(ProcessedMessage).where(
                and_(
                    ProcessedMessage.chat_title == chat_title,
                    ProcessedMessage.message_id == message_id,
                )
            )
        )
        return result.scalar_one_or_none() is not None


async def _mark_msg_processed(chat_title: str, message_id: int, tenant_id: int = None):
    async with async_session() as session:
        session.add(ProcessedMessage(tenant_id=tenant_id, chat_title=chat_title, message_id=message_id))
        await session.commit()


async def _cleanup_old_processed():
    cutoff = datetime.now(timezone.utc) - timedelta(days=_CLEANUP_DAYS)
    async with async_session() as session:
        await session.execute(
            delete(ProcessedMessage).where(ProcessedMessage.created_at < cutoff)
        )
        await session.commit()


def _add_context(user_id: int, text: str):
    if user_id not in _user_context:
        if len(_user_context) >= MAX_CONTEXT_USERS:
            oldest = next(iter(_user_context))
            del _user_context[oldest]
        _user_context[user_id] = []
    _user_context[user_id].append(text)
    if len(_user_context[user_id]) > MAX_CONTEXT:
        _user_context[user_id] = _user_context[user_id][-MAX_CONTEXT:]


def _get_context(user_id: int) -> Optional[str]:
    ctx = _user_context.get(user_id, [])
    if len(ctx) < 2:
        return None
    return "\n".join(ctx[:-1])


def _extract_user_info(sender) -> dict:
    if not sender:
        return {
            "user_id": None,
            "username": None,
            "first_name": None,
            "last_name": None,
            "profile_link": None,
        }

    info = {
        "user_id": sender.id,
        "username": getattr(sender, "username", None),
        "first_name": getattr(sender, "first_name", None),
        "last_name": getattr(sender, "last_name", None),
    }
    if info["username"]:
        info["profile_link"] = f"https://t.me/{info['username']}"
    else:
        info["profile_link"] = f"tg://user?id={info['user_id']}"
    return info


async def _round_robin_assign(session) -> int | None:
    from sqlalchemy import select, func, and_
    from app.models import Lead, User

    result = await session.execute(
        select(User.id, func.count(Lead.id).label("cnt"))
        .outerjoin(Lead, Lead.assigned_to == User.id)
        .where(User.role.in_(["manager", "admin", "super_admin"]), User.is_active == True)
        .group_by(User.id)
        .order_by(func.count(Lead.id).asc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


async def _save_lead(
    user_info: dict,
    text: str,
    chat_title: str,
    chat_username: str,
    message_id: int,
    analysis: dict,
    reply_to_id: int | None = None,
    reply_to_text: str | None = None,
    tenant_id: int | None = None,
) -> Lead:
    async with async_session() as session:
        assigned_to = await _round_robin_assign(session)

        lead = Lead(
            tenant_id=tenant_id,
            user_id=user_info["user_id"],
            username=user_info["username"],
            first_name=user_info["first_name"],
            last_name=user_info["last_name"],
            profile_link=user_info["profile_link"],
            message_text=text,
            reply_to_id=reply_to_id,
            reply_to_text=reply_to_text,
            chat_title=chat_title,
            chat_username=chat_username,
            message_id=message_id,
            lead_score=analysis["lead_score"],
            urgency=analysis["urgency"],
            reason=analysis.get("reason", ""),
            recommended_message=analysis.get("recommended_message", ""),
            phone=analysis.get("phone"),
            hotness=analysis.get("hotness", "cold"),
            ai_summary=analysis.get("ai_summary"),
            next_action=analysis.get("next_action"),
            budget=analysis.get("budget"),
            timeline=analysis.get("timeline"),
            readiness=analysis.get("readiness"),
            city=analysis.get("city"),
            status="new",
            assigned_to=assigned_to,
        )
        session.add(lead)
        await session.flush()

        history = LeadHistory(
            lead_id=lead.id,
            tenant_id=tenant_id,
            user_id=assigned_to,
            action="created",
            note="Автоматическое создание из Telegram",
        )
        session.add(history)

        # Record assignment for response time tracking
        if assigned_to:
            from app.repositories import ResponseTimeRepository
            resp_repo = ResponseTimeRepository(session, tenant_id)
            await resp_repo.record_assignment(assigned_to, lead.id)

        await session.commit()
        await session.refresh(lead)
        return lead


async def _get_forum_topics(client: TelegramClient, chat_entity) -> list:
    """Get all topics from a forum group."""
    topics = []
    try:
        from telethon.tl.functions.messages import GetForumTopicsRequest
        result = await client(GetForumTopicsRequest(
            channel=chat_entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
        ))
        for topic in result.topics:
            if not topic.hidden:
                topics.append({
                    "id": topic.id,
                    "title": topic.title,
                })
        logger.info("Got %d topics via API for %s", len(topics), getattr(chat_entity, 'title', '?'))
    except Exception as e:
        logger.warning("GetForumTopicsRequest failed for %s: %s", getattr(chat_entity, 'title', '?'), e)
    return topics


async def _scan_history(client: TelegramClient, chat_entity, chat_title: str, chat_username: str, tenant_config: TenantConfig = None, tenant_id: int = None):
    global _user_context

    if tenant_config is None:
        tenant_config = TenantConfig()

    keywords = tenant_config.keywords
    noise_keywords = tenant_config.noise_keywords
    min_score = tenant_config.min_lead_score
    require_keywords = tenant_config.require_keywords
    await _cleanup_old_processed()

    scan_state = _load_scan_state()
    chat_key = chat_username or chat_title
    last_msg_id = scan_state.get(chat_key, 0)

    is_first_scan = last_msg_id == 0
    scan_label = "full history" if is_first_scan else f"incremental (since msg#{last_msg_id})"
    logger.info("Scanning %s — %s...", chat_title, scan_label)

    count = 0
    leads_found = 0
    total_iterated = 0
    ai_calls = 0
    MAX_AI_CALLS_PER_CHAT = 200
    skipped_text = 0
    skipped_noise = 0
    skipped_ad = 0
    skipped_keywords = 0
    skipped_processed = 0
    max_seen_id = last_msg_id
    analyzed_max_id = last_msg_id  # advance only past messages that went through analysis

    is_forum = getattr(chat_entity, "is_forum", False)
    topics = []

    if is_forum:
        topics = await _get_forum_topics(client, chat_entity)
        if topics:
            logger.info("Found %d topics in forum %s", len(topics), chat_title)

    topic_map = {t["id"]: t["title"] for t in topics} if topics else {}

    iter_kwargs = {"reverse": True}
    if not is_first_scan:
        iter_kwargs["min_id"] = last_msg_id

    try:
        async for message in client.iter_messages(
            chat_entity,
            **iter_kwargs,
        ):
            total_iterated += 1
            if message.id > max_seen_id:
                max_seen_id = message.id

            text = message.text or ""
            
            if not text and (message.voice or message.video_note):
                transcribed = await _transcribe_voice(message, tenant_id=tenant_id)
                if transcribed:
                    text = transcribed

            if not text:
                skipped_text += 1
                continue

            if _is_noise(text, noise_keywords):
                skipped_noise += 1
                continue

            if _is_ad(text):
                skipped_ad += 1
                continue

            if require_keywords and not _matches_keywords(text, keywords):
                skipped_keywords += 1
                continue

            if not require_keywords and not _matches_keywords(text, keywords) and len(text) < 30:
                skipped_text += 1
                continue

            if await _is_msg_processed(chat_title, message.id):
                skipped_processed += 1
                continue

            await _mark_msg_processed(chat_title, message.id, tenant_id)
            count += 1

            thread_id = getattr(message, "message_thread_id", None)
            topic_label = chat_title
            if thread_id and thread_id in topic_map:
                topic_label = f"{chat_title} → {topic_map[thread_id]}"
            elif thread_id and is_forum:
                topic_label = f"{chat_title} → Topic {thread_id}"

            sender = await message.get_sender()
            user_info = _extract_user_info(sender)
            user_id = user_info.get("user_id")

            if user_id and await _is_duplicate(user_id, tenant_id):
                continue

            if user_id:
                _add_context(user_id, text)
                context = _get_context(user_id)
                await _save_user_message(tenant_id, user_id, user_info.get("username"),
                                          user_info.get("first_name"), chat_title, message.id, text)
                db_history = await _get_user_history(tenant_id, user_id, chat_title, limit=10)
                if db_history and len(db_history) > 1:
                    context = "\n".join(db_history[:-1])
            else:
                context = None

            reply_to_id = None
            reply_to_text = None
            if message.reply_to and getattr(message.reply_to, "reply_to_msg_id", None):
                try:
                    reply_to_id = message.reply_to.reply_to_msg_id
                    reply_msg = await client.get_messages(chat_entity, ids=reply_to_id)
                    if reply_msg and reply_msg.text:
                        reply_to_text = reply_msg.text[:2000]
                except Exception:
                    pass

            if ai_calls >= MAX_AI_CALLS_PER_CHAT:
                logger.info("AI call limit reached for %s (%d)", chat_title, MAX_AI_CALLS_PER_CHAT)
                break

            analysis = await analyze_message(text, topic_label, context, reply_to_text=reply_to_text,
                                               tenant_config=tenant_config, tenant_id=tenant_id,
                                               user_name=user_info.get("first_name"))
            ai_calls += 1
            analyzed_max_id = max(analyzed_max_id, message.id)
            await asyncio.sleep(0.3)

            if not analysis.get("is_lead"):
                continue
            if analysis["lead_score"] < min_score:
                continue

            lead = await _save_lead(
                user_info, text, topic_label, chat_username, message.id, analysis,
                reply_to_id=reply_to_id, reply_to_text=reply_to_text, tenant_id=tenant_id,
            )
            leads_found += 1

            logger.info(
                "History lead: %s (score=%d) from %s",
                user_info.get("username") or user_id,
                analysis["lead_score"],
                topic_label,
            )

    except Exception as e:
        logger.error("Error iterating messages for %s: %s", chat_title, e)

    # If AI limit hit, don't advance past messages that were never analyzed —
    # they'll be picked up (and cheaply skipped via ProcessedMessage) next scan.
    save_id = analyzed_max_id if ai_calls >= MAX_AI_CALLS_PER_CHAT else max_seen_id
    if save_id > last_msg_id:
        scan_state[chat_key] = save_id
        _save_scan_state(scan_state)

    logger.info(
        "Scan complete for %s: iterated=%d, noise=%d, ad=%d, no_kw=%d, processed=%d, ai=%d, leads=%d",
        chat_title, total_iterated, skipped_noise, skipped_ad, skipped_keywords, skipped_processed, ai_calls, leads_found,
    )


async def _scan_chat(client: TelegramClient, chat: str, tenant_config: TenantConfig = None, tenant_id: int = None):
    async with _SCAN_SEMAPHORE:
        try:
            if chat.startswith("https://t.me/") or chat.startswith("t.me/"):
                username = chat.split("/")[-1]
                entity = await client.get_entity(username)
            else:
                entity = await client.get_entity(chat)

            if isinstance(entity, (Channel, Chat)):
                title = getattr(entity, "title", chat)
                username = getattr(entity, "username", "") or ""
                await _scan_history(client, entity, title, username, tenant_config, tenant_id)
            else:
                title = chat
                username = ""
                await _scan_history(client, entity, title, username, tenant_config, tenant_id)
        except Exception:
            logger.exception("Failed to scan history for %s", chat)
        finally:
            await asyncio.sleep(2)


async def start_monitor(client: TelegramClient, chats: list[str], tenant_config: TenantConfig = None, tenant_id: int = None):
    if tenant_config is None:
        tenant_config = TenantConfig()

    keywords = tenant_config.keywords
    noise_keywords = tenant_config.noise_keywords
    min_score = tenant_config.min_lead_score
    require_keywords = tenant_config.require_keywords

    if not client.is_connected():
        await client.start()

    await asyncio.gather(*[_scan_chat(client, chat, tenant_config, tenant_id) for chat in chats])

    resolved_entities = []
    for chat in chats:
        try:
            if chat.startswith("https://t.me/") or chat.startswith("t.me/"):
                username = chat.split("/")[-1]
                entity = await client.get_entity(username)
            else:
                entity = await client.get_entity(chat)
            resolved_entities.append(entity)
        except Exception:
            logger.warning("Could not resolve entity for live monitoring: %s", chat)

    @client.on(events.NewMessage(chats=resolved_entities))
    async def handler(event):
        try:
            text = event.message.text or ""
            
            if not text and (event.message.voice or event.message.video_note):
                transcribed = await _transcribe_voice(event.message, tenant_id=tenant_id)
                if transcribed:
                    text = transcribed
            
            if not text.strip():
                return

            if _is_noise(text, noise_keywords):
                return

            if _is_ad(text):
                return

            if require_keywords and not _matches_keywords(text, keywords):
                return

            if not require_keywords and not _matches_keywords(text, keywords) and len(text) < 30:
                return

            sender = await event.get_sender()
            user_info = _extract_user_info(sender)
            user_id = user_info.get("user_id")

            if user_id and await _is_duplicate(user_id, tenant_id):
                return

            if user_id:
                _add_context(user_id, text)
                context = _get_context(user_id)
            else:
                context = None

            chat = await event.get_chat()
            chat_title = getattr(chat, "title", "Unknown")
            chat_username = getattr(chat, "username", None) or ""

            topic_id = getattr(event.message, "message_thread_id", None)
            is_forum = getattr(chat, "is_forum", False)

            if is_forum and topic_id:
                chat_title = f"{chat_title} → Topic {topic_id}"

            await _save_user_message(tenant_id, user_id, user_info.get("username"),
                                      user_info.get("first_name"), chat_title,
                                      event.message.id, text)
            db_history = await _get_user_history(tenant_id, user_id, chat_title, limit=10)
            if db_history and len(db_history) > 1:
                context = "\n".join(db_history[:-1])

            reply_to_id = None
            reply_to_text = None
            if event.message.reply_to and getattr(event.message.reply_to, "reply_to_msg_id", None):
                try:
                    reply_to_id = event.message.reply_to.reply_to_msg_id
                    reply_msg = await client.get_messages(chat, ids=reply_to_id)
                    if reply_msg and reply_msg.text:
                        reply_to_text = reply_msg.text[:2000]
                except Exception:
                    pass

            analysis = await analyze_message(text, chat_title, context, reply_to_text=reply_to_text,
                                              tenant_config=tenant_config, tenant_id=tenant_id,
                                              user_name=user_info.get("first_name"))

            if not analysis.get("is_lead"):
                return
            if analysis["lead_score"] < min_score:
                return

            async with async_session() as session:
                existing_q = select(Lead).where(
                    Lead.tenant_id == tenant_id,
                    Lead.user_id == user_info["user_id"],
                    Lead.status.notin_(["deleted", "archive", "not_interested"]),
                ).order_by(Lead.created_at.desc()).limit(1)
                existing = (await session.execute(existing_q)).scalar_one_or_none()
                if existing:
                    age_hours = (utcnow() - existing.created_at.replace(tzinfo=None)).total_seconds() / 3600
                    if age_hours < 72:
                        logger.info("Skipping duplicate lead from user %s (existing #%d, %dh old)", user_info.get("username") or user_info["user_id"], existing.id, int(age_hours))
                        return

            lead = await _save_lead(
                user_info, text, chat_title, chat_username, event.message.id, analysis,
                reply_to_id=reply_to_id, reply_to_text=reply_to_text, tenant_id=tenant_id,
            )

            await send_lead_notification(lead)

            logger.info(
                "New lead: %s (score=%d) from %s",
                user_info.get("username") or user_id,
                analysis["lead_score"],
                chat_title,
            )

        except Exception:
            logger.exception("Error processing message")

    logger.info("Monitoring %d chats via Telethon (%d resolved for live)", len(chats), len(resolved_entities))
    await client.run_until_disconnected()
