"""Auto-discovery of public residential-complex (ЖК) Telegram chats.

Searches global Telegram directory via contacts.SearchRequest for queries
built around the tenant's city, filters to megagroups with usernames,
and stores new candidates as ChatSuggestion rows (status=pending).
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

_MAX_CANDIDATES_PER_RUN = 8
_SEARCH_DELAY = 2.0  # seconds between SearchRequest calls
_TITLE_HINTS = ("жк", "новострой", "жилой комплекс", "дом ", "дом|")


def _build_queries(city: str) -> list[str]:
    city = (city or "").strip()
    if city:
        return [
            f"ЖК {city}",
            f"новостройки {city}",
            f"жилой комплекс {city}",
        ]
    return ["ЖК новостройка"]


def _is_relevant(title: str, query: str) -> bool:
    t = (title or "").lower()
    if any(h in t for h in _TITLE_HINTS):
        return True
    # Query already contained ЖК/новострой — accept broader matches
    q = query.lower()
    return "жк" in q or "новострой" in q


async def discover_chats_for_tenant(tenant_id: int, session_name: str,
                                    city: str = "", monitored: list[str] | None = None):
    """Run one discovery pass. Returns number of new suggestions saved."""
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import Channel

    from app.telegram_factory import client_factory
    from app.models import ChatSuggestion, async_session
    from sqlalchemy import select

    monitored_set = {(c or "").rstrip("/").lower() for c in (monitored or [])}

    client = client_factory.create_client(tenant_id, session_name)
    candidates = {}
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("Chat discovery: session %s not authorized", session_name)
            await client.disconnect()
            return 0

        for query in _build_queries(city)[:3]:
            try:
                result = await client(SearchRequest(q=query, limit=30))
            except Exception as e:
                logger.warning("Chat discovery search '%s' failed: %s", query, e)
                continue
            for ch in getattr(result, "chats", []) or []:
                if not isinstance(ch, Channel) or not ch.megagroup or not ch.username:
                    continue
                uname = ch.username.lower()
                url = f"https://t.me/{ch.username}"
                if uname in candidates or url.lower() in monitored_set or uname in monitored_set:
                    continue
                if not _is_relevant(ch.title, query):
                    continue
                members = getattr(ch, "participants_count", None) or 0
                candidates[uname] = {
                    "title": (ch.title or "")[:250],
                    "username": ch.username,
                    "members_count": int(members),
                    "match_query": query[:100],
                }
            await asyncio.sleep(_SEARCH_DELAY)

        # Enrich top candidates with member counts (full channel info), capped
        from telethon.tl.functions.channels import GetFullChannelRequest
        enriched = []
        for uname, data in list(candidates.items())[:_MAX_CANDIDATES_PER_RUN]:
            try:
                full = await client(GetFullChannelRequest(uname))
                data["members_count"] = int(getattr(full.full_chat, "participants_count", 0) or 0)
            except Exception:
                pass
            enriched.append(data)
            await asyncio.sleep(0.5)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if not enriched:
        return 0

    saved = 0
    async with async_session() as session:
        for data in enriched:
            exists = (await session.execute(
                select(ChatSuggestion).where(
                    ChatSuggestion.tenant_id == tenant_id,
                    ChatSuggestion.username == data["username"],
                )
            )).scalar_one_or_none()
            if exists:
                if data["members_count"] and not exists.members_count:
                    exists.members_count = data["members_count"]
                continue
            session.add(ChatSuggestion(
                tenant_id=tenant_id,
                title=data["title"],
                username=data["username"],
                members_count=data["members_count"],
                match_query=data["match_query"],
            ))
            saved += 1
        await session.commit()
    if saved:
        logger.info("Chat discovery: %d new suggestions for tenant %d", saved, tenant_id)
    return saved
