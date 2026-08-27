"""Base scraper class for all lead sources."""
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import Optional

logger = logging.getLogger(__name__)


def retry_on_error(max_retries=3, backoff=2):
    """Decorator for retry with exponential backoff."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        wait = backoff ** attempt
                        logger.warning("Scraper error (attempt %d/%d), retrying in %ds: %s",
                                      attempt + 1, max_retries, wait, e)
                        await asyncio.sleep(wait)
            logger.error("Scraper failed after %d attempts: %s", max_retries, last_error)
            raise last_error
        return wrapper
    return decorator


@dataclass
class ScrapedLead:
    """Unified lead format from any source."""
    source: str  # "vk", "avito", "cian", "forumhouse"
    source_id: str  # unique ID in the source
    source_url: str  # link to original post
    text: str  # message text
    author_name: str = ""
    author_username: str = ""
    author_id: str = ""
    city: str = ""
    category: str = ""  # "repair", "construction", "design", etc.
    budget: Optional[float] = None
    urgency: str = "low"  # "low", "medium", "high"
    keywords_matched: list = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


class BaseScraper(ABC):
    """Base class for all scrapers."""

    SOURCE_NAME: str = "unknown"

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.logger = logging.getLogger(f"scraper.{self.SOURCE_NAME}")
        self._errors = []

    def log_error(self, error: str):
        self._errors.append({"time": datetime.utcnow().isoformat(), "error": error})
        if len(self._errors) > 50:
            self._errors = self._errors[-50:]

    def get_errors(self) -> list:
        return self._errors[-10:]

    @abstractmethod
    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search for leads matching query."""
        pass

    @abstractmethod
    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor for new leads (called periodically)."""
        pass

    def _classify_urgency(self, text: str) -> str:
        """Classify urgency from text."""
        text_lower = text.lower()
        high_signals = ["срочно", "быстро", "немедленно", "завтра", "на этой неделе", "экстренно"]
        medium_signals = ["скоро", "в ближайшее время", "в этом месяце", "к лету", "к зиме"]
        low_signals = ["когда-нибудь", "в будущем", "пока думаю", "просто интересуюсь"]

        for signal in high_signals:
            if signal in text_lower:
                return "high"
        for signal in medium_signals:
            if signal in text_lower:
                return "medium"
        return "low"

    def _estimate_budget(self, text: str) -> Optional[float]:
        """Try to extract budget from text."""
        import re
        text_lower = text.lower()

        # Patterns: "бюджет 500 тысяч", "около 300 тыс", "до 1 миллиона"
        patterns = [
            r'бюджет[:\s]*(\d+[\s]*(?:тыс|тысяч|млн|миллион|к|000)?)',
            r'около[:\s]*(\d+[\s]*(?:тыс|тысяч|млн|миллион|к|000)?)',
            r'до[:\s]*(\d+[\s]*(?:тыс|тысяч|млн|миллион|к|000)?)',
            r'(\d+[\s]*(?:тыс|тысяч|к|000))\s*(?:рублей|₽|руб)',
            r'(\d+[\s]*(?:млн|миллион))\s*(?:рублей|₽|руб)?',
        ]

        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                num_str = match.group(1).replace(' ', '')
                try:
                    num = float(re.sub(r'[^\d.]', '', num_str))
                    if 'млн' in text_lower or 'миллион' in text_lower:
                        num *= 1_000_000
                    elif 'тыс' in text_lower or 'тысяч' in text_lower or 'к' in text_lower:
                        num *= 1_000
                    elif num < 1000:
                        num *= 1_000
                    return num
                except (ValueError, TypeError):
                    continue
        return None

    def _detect_category(self, text: str) -> str:
        """Detect renovation category from text."""
        text_lower = text.lower()
        categories = {
            "kitchen": ["кухня", "кухни", "кухню"],
            "bathroom": ["ванная", "ванной", "ванную", "санузел", "туалет"],
            "apartment": ["квартира", "квартиры", "квартиру", "студия"],
            "house": ["дом", "дома", "дача", "коттедж"],
            "office": ["офис", "офиса", "офисное", "коммерческое"],
            "cosmetic": ["косметический", "косметика", "обои", "покраска"],
            "capital": ["капитальный", "капитальный ремонт", "перепланировка"],
            "design": ["дизайн", "дизайнерский", "проект"],
        }
        for cat, keywords in categories.items():
            for kw in keywords:
                if kw in text_lower:
                    return cat
        return "general"
