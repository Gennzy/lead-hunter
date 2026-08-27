"""ForumHouse scraper for renovation discussions."""
import logging
import re
import aiohttp
from typing import Optional
from .base import BaseScraper, ScrapedLead, retry_on_error

logger = logging.getLogger(__name__)

# ForumHouse sections relevant to renovation
RELEVANT_SECTIONS = [
    "remont-i-otdelka",  # Ремонт и отделка
    "stroitelstvo",      # Строительство
    "inzhenernye-sistemy", # Инженерные системы
    "interier",          # Интерьер
    "plotnie-raboty",    # Плотницкие работы
    "elektrika",         # Электрика
    "vodosnabzhenie",    # Водоснабжение
    "otoplenie",         # Отопление
    "ventilyatsiya",     # Вентиляция
]


class ForumHouseScraper(BaseScraper):
    """Scrape ForumHouse for renovation discussions and leads."""

    SOURCE_NAME = "forumhouse"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.base_url = "https://www.forumhouse.ru"
        self.search_url = f"{self.base_url}/search/"

    @retry_on_error(max_retries=2, backoff=3)
    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search ForumHouse for renovation discussions."""
        leads = []

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }

                # Search in forum threads
                params = {
                    "q": query,
                    "type": "thread",
                    "order": "date",
                }

                async with session.get(
                    self.search_url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        leads = self._parse_search_results(html, query)
                    else:
                        self.logger.warning("ForumHouse search returned status %d", resp.status)

        except Exception as e:
            self.logger.error("ForumHouse search failed: %s", e)
            self.log_error(str(e))

        return leads[:limit]

    @retry_on_error(max_retries=2, backoff=3)
    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor ForumHouse for renovation discussions."""
        all_leads = []
        search_queries = queries or [
            "ищу бригаду для ремонта",
            "нужна бригада ремонт",
            "сколько стоит ремонт",
            "ремонт квартиры",
            "капитальный ремонт",
            "косметический ремонт",
        ]

        for query in search_queries:
            leads = await self.search(query)
            all_leads.extend(leads)

        # Deduplicate
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.source_id not in seen:
                seen.add(lead.source_id)
                unique_leads.append(lead)

        self.logger.info("ForumHouse: found %d unique leads from %d queries", len(unique_leads), len(search_queries))
        return unique_leads

    async def scrape_section(self, section: str, limit: int = 50) -> list[ScrapedLead]:
        """Scrape a specific ForumHouse section."""
        leads = []
        url = f"{self.base_url}/forums/{section}/"

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }

                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        leads = self._parse_section_page(html, section)

        except Exception as e:
            self.logger.error("ForumHouse section scrape failed: %s", e)

        return leads[:limit]

    async def monitor_sections(self, sections: list[str] = None, limit_per_section: int = 30) -> list[ScrapedLead]:
        """Monitor multiple ForumHouse sections."""
        all_leads = []
        target_sections = sections or RELEVANT_SECTIONS

        for section in target_sections:
            leads = await self.scrape_section(section, limit=limit_per_section)
            all_leads.extend(leads)
            self.logger.info("ForumHouse section %s: found %d leads", section, len(leads))

        # Deduplicate
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.source_id not in seen:
                seen.add(lead.source_id)
                unique_leads.append(lead)

        return unique_leads

    def _parse_search_results(self, html: str, query: str) -> list[ScrapedLead]:
        """Parse ForumHouse search results."""
        leads = []

        # Extract thread links and titles
        thread_pattern = re.compile(
            r'<a[^>]+href="(/threads/[^"]+)"[^>]*>([^<]+)</a>',
            re.IGNORECASE
        )

        for match in thread_pattern.finditer(html):
            thread_path = match.group(1)
            title = match.group(2).strip()

            if not title or len(title) < 10:
                continue

            # Check if thread is about seeking renovation help
            if self._is_seeking_thread(title):
                lead = ScrapedLead(
                    source="forumhouse",
                    source_id=f"fh_{thread_path.split('/')[-1]}",
                    source_url=f"{self.base_url}{thread_path}",
                    text=title,
                    category=self._detect_category(title),
                    urgency=self._classify_urgency(title),
                    keywords_matched=self._get_matched_keywords(title),
                    raw_data={"query": query, "path": thread_path},
                )
                leads.append(lead)

        return leads

    def _parse_section_page(self, html: str, section: str) -> list[ScrapedLead]:
        """Parse a ForumHouse section page."""
        leads = []

        # Extract thread listings
        thread_pattern = re.compile(
            r'<a[^>]+href="(/threads/[^"]+)"[^>]*class="[^"]*"[^>]*>([^<]+)</a>',
            re.IGNORECASE
        )

        for match in thread_pattern.finditer(html):
            thread_path = match.group(1)
            title = match.group(2).strip()

            if not title or len(title) < 10:
                continue

            if self._is_seeking_thread(title):
                lead = ScrapedLead(
                    source="forumhouse",
                    source_id=f"fh_{section}_{thread_path.split('/')[-1]}",
                    source_url=f"{self.base_url}{thread_path}",
                    text=title,
                    category=self._detect_category(title),
                    urgency=self._classify_urgency(title),
                    keywords_matched=self._get_matched_keywords(title),
                    raw_data={"section": section, "path": thread_path},
                )
                leads.append(lead)

        return leads

    def _is_seeking_thread(self, title: str) -> bool:
        """Check if thread title indicates someone seeking renovation help."""
        title_lower = title.lower()

        # Seeking signals
        seeking_patterns = [
            r"ищу\s+(?:бригаду|мастеров|подрядчика)",
            r"нужн[аоы]\s+(?:бригада|мастер|подрядчик|помощь)",
            r"как\s+(?:сделать|сделать ремонт)",
            r"сколько\s+(?:стоит|будет стоить)",
            r"посоветуйте",
            r"рекомендуйте",
            r"помогите\s+(?:с|выбрать)",
            r"вопрос\s+по\s+ремонту",
            r"нужен\s+(?:совет|помощь|рекомендация)",
        ]

        for pattern in seeking_patterns:
            if re.search(pattern, title_lower):
                return True

        # Check if it's a question about renovation
        question_words = ["как", "почему", "сколько", "где", "когда", "что", "чем", "помогите", "совет"]
        if any(w in title_lower for w in question_words):
            renovation_words = ["ремонт", "отделка", "строительство", "плитка", "электрика", "сантехника"]
            if any(w in title_lower for w in renovation_words):
                return True

        return False

    def _get_matched_keywords(self, text: str) -> list[str]:
        """Get list of matched renovation keywords."""
        text_lower = text.lower()
        keywords = [
            "ремонт", "отделка", "бригада", "мастер", "подрядчик",
            "квартира", "кухня", "ванная", "дом", "капитальный",
            "косметический", "евроремонт", "дизайн", "плитка",
            "электрика", "сантехника", "гипсокартон", "стяжка",
            "утепление", "звукоизоляция", "перепланировка",
        ]
        return [kw for kw in keywords if kw in text_lower]
