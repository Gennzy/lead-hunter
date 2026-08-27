from __future__ import annotations

import logging
from typing import Optional
from config import settings, KEYWORDS, NOISE_KEYWORDS, KEYWORDS_LIST, NOISE_KEYWORDS_LIST

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = """Ты — AI-аналитик для компании по ремонту квартир.
Мы делаем ПОЛНЫЙ РЕМОНТ КВАРТИР ПОД КЛЮЧ — от черновой отделки до чистовой, включая все виды работ.

Анализируешь сообщения из Telegram-чатов новостроек и определяешь, является ли автор потенциальным клиентом на ПОЛНЫЙ РЕМОНТ КВАРТИРЫ.

Контекст: Тебе передаётся ТЕКСТ СООБЩЕНИЯ и reply_to — на что человек отвечает. Используй это для понимания смысла.

СТРОГО НЕ считай лидами (всегда is_lead=false, score=0-30):
- Жалобы на шум, стройку, соседей, капремонт
- Комментарии о ходе строительства ("ничего не делают", "громыхают", "ничего не меняется")
- Обсуждение ЖКХ, квитанций, задолженностей
- Приёмка квартиры от застройщика (дефекты, акты, замечания)
- Общие разговоры, болтовня, мемы, шутки
- Рекламные посты, предложения услуг
- Поиск работы / вакансии
- Вопросы о районе, инфраструктуре, парковке
- Обсуждение цен на квартиры (покупка/продажа)
- Новости о застройщике, сдаче дома

СЧИТАЙ ЛИДАМИ (is_lead=true, score=70-100):
- Ищет бригаду/мастера/подрядчика ДЛЯ РЕМОНТА СВОЕЙ КВАРТИРЫ
- Получил ключи и ПЛАНИРУЕТ РЕМОНТ (не просто получил ключи)
- Спрашивает стоимость РЕМОНТА/ОТДЕЛКИ своей квартиры
- Хочет сделать РЕМОНТ ПОД КЛЮЧ своей квартиры
- Нужна помощь с выбором подрядчика/материалов ДЛЯ РЕМОНТА
- Конкретно планирует ремонт: "хочу сделать ремонт", "ищу мастеров для ремонта"

ВАЖНО: Просто упоминание слова "ремонт" НЕ делает сообщение лидом. Человек должен ЯВНО выражать НАМЕРЕНИЕ сделать ремонт в своей квартире.

Оцени по шкале 0-100:
- 90-100: Явный лид (ищет подрядчика для своего ремонта, получил ключи и планирует)
- 70-89: Возможный лид (упоминает планы по ремонту)
- 0-69: Не лид

Ответ ТОЛЬКО в JSON:
{"is_lead": true/false, "lead_score": 0-100, "urgency": "low/medium/high", "reason": "краткое объяснение", "recommended_message": "персонализированное сообщение 2-3 предложения"}"""

_DEFAULT_CITY = "Санкт-Петербург"


class TenantConfig:
    """Per-tenant configuration loaded from Tenant.config JSONB."""

    def __init__(self, raw_config: dict | None = None, tenant_id: int | None = None):
        if raw_config:
            self._config = raw_config
        elif tenant_id:
            self._config = self._load_from_db(tenant_id)
        else:
            self._config = {}

    @classmethod
    async def create(cls, tenant_id: int | None) -> "TenantConfig":
        """Correct way to build a TenantConfig from inside async code
        (route handlers, monitor tasks, etc). Always awaits the DB load —
        never silently falls back to an empty config."""
        if tenant_id is None:
            return cls()
        raw = await cls._async_load(tenant_id)
        return cls(raw_config=raw)

    def _load_from_db(self, tenant_id: int) -> dict:
        """Sync loader — ONLY safe to use outside a running event loop
        (e.g. plain scripts). Inside async code, use `await TenantConfig.create(tenant_id)`."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                logger.warning(
                    "TenantConfig(tenant_id=%s) called from within a running event loop — "
                    "config will be EMPTY. Use `await TenantConfig.create(tenant_id)` instead.",
                    tenant_id,
                )
                return {}
            return loop.run_until_complete(self._async_load(tenant_id))
        except Exception:
            return {}

    @staticmethod
    async def _async_load(tenant_id: int) -> dict:
        from app.models import Tenant, async_session
        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = result.scalar_one_or_none()
            return tenant.config if tenant else {}

    @property
    def keywords(self) -> set[str]:
        custom = self._config.get("keywords")
        if custom and isinstance(custom, list):
            return set(kw.lower() for kw in custom)
        return KEYWORDS

    @property
    def keywords_list(self) -> list[str]:
        custom = self._config.get("keywords")
        if custom and isinstance(custom, list):
            return custom
        return KEYWORDS_LIST

    @property
    def noise_keywords(self) -> set[str]:
        custom = self._config.get("noise_keywords")
        if custom and isinstance(custom, list):
            return set(kw.lower() for kw in custom)
        return NOISE_KEYWORDS

    @property
    def noise_keywords_list(self) -> list[str]:
        custom = self._config.get("noise_keywords")
        if custom and isinstance(custom, list):
            return custom
        return NOISE_KEYWORDS_LIST

    @property
    def system_prompt(self) -> str:
        return self._config.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT

    @property
    def min_lead_score(self) -> int:
        return self._config.get("min_lead_score", settings.min_lead_score)

    @property
    def require_keywords(self) -> bool:
        return self._config.get("require_keywords", False)

    @property
    def city(self) -> str:
        return self._config.get("city", _DEFAULT_CITY)

    @property
    def company_name(self) -> str:
        return self._config.get("company_name", "")

    @property
    def bot_token(self) -> str:
        return self._config.get("bot_token", settings.bot_token)

    @property
    def owner_chat_id(self) -> int:
        return self._config.get("owner_chat_id", settings.owner_chat_id)

    @property
    def monitored_chats(self) -> list[str]:
        chats = self._config.get("monitored_chats", "")
        if isinstance(chats, list):
            return chats
        if isinstance(chats, str) and chats:
            return [c.strip() for c in chats.split(",") if c.strip()]
        return settings.get_monitored_chats()

    @property
    def chats(self) -> list[str]:
        """Alias for monitored_chats."""
        return self.monitored_chats

    @property
    def theme_color(self) -> str:
        return self._config.get("theme_color", "#7c5832")

    @property
    def logo_url(self) -> str | None:
        return self._config.get("logo_url")

    @property
    def favicon_url(self) -> str:
        return self._config.get("favicon_url", "/static/favicon.svg")

    # Billing limits
    @property
    def max_ai_requests_per_day(self) -> int:
        return self._config.get("max_ai_requests_per_day", 100)

    @property
    def max_tokens_per_day(self) -> int:
        return self._config.get("max_tokens_per_day", 500000)

    @property
    def max_cost_per_month_usd(self) -> float:
        return self._config.get("max_cost_per_month_usd", 50.0)

    @property
    def max_leads_per_month(self) -> int:
        return self._config.get("max_leads_per_month", 500)

    @property
    def ai_enabled(self) -> bool:
        return self._config.get("ai_enabled", True)

    def to_dict(self) -> dict:
        return dict(self._config)

    def update(self, data: dict):
        self._config.update(data)

    # Plan limits
    @property
    def plan(self) -> str:
        return self._config.get("plan", "free")

    @property
    def max_users(self) -> int:
        limits = {"free": 3, "pro": 10, "enterprise": 999}
        return limits.get(self.plan, 3)

    @property
    def max_chats(self) -> int:
        limits = {"free": 5, "pro": 20, "enterprise": 999}
        return limits.get(self.plan, 5)

    @property
    def plan_features(self) -> dict:
        return {
            "free": {
                "name": "Free",
                "price": 0,
                "max_users": 3,
                "max_chats": 5,
                "max_leads": 100,
                "ai_scoring": True,
                "api_access": False,
                "custom_webhooks": False,
                "priority_support": False,
            },
            "pro": {
                "name": "Pro",
                "price": 9900,
                "max_users": 10,
                "max_chats": 20,
                "max_leads": 1000,
                "ai_scoring": True,
                "api_access": True,
                "custom_webhooks": True,
                "priority_support": False,
            },
            "enterprise": {
                "name": "Enterprise",
                "price": 29900,
                "max_users": 999,
                "max_chats": 999,
                "max_leads": 999999,
                "ai_scoring": True,
                "api_access": True,
                "custom_webhooks": True,
                "priority_support": True,
            },
        }.get(self.plan, {
                "name": "Free",
                "price": 0,
                "max_users": 3,
                "max_chats": 5,
                "max_leads": 100,
                "ai_scoring": True,
                "api_access": False,
                "custom_webhooks": False,
                "priority_support": False,
            })

    @property
    def all_plans(self) -> dict:
        return {
            "free": {
                "name": "Free",
                "price": 0,
                "max_users": 3,
                "max_chats": 5,
                "max_leads": 100,
                "ai_scoring": True,
                "api_access": False,
                "custom_webhooks": False,
                "priority_support": False,
            },
            "pro": {
                "name": "Pro",
                "price": 9900,
                "max_users": 10,
                "max_chats": 20,
                "max_leads": 1000,
                "ai_scoring": True,
                "api_access": True,
                "custom_webhooks": True,
                "priority_support": False,
            },
            "enterprise": {
                "name": "Enterprise",
                "price": 29900,
                "max_users": 999,
                "max_chats": 999,
                "max_leads": 999999,
                "ai_scoring": True,
                "api_access": True,
                "custom_webhooks": True,
                "priority_support": True,
            },
        }
