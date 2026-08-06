"""Avito scraper for renovation leads."""
import logging
import re
import aiohttp
from typing import Optional
from .base import BaseScraper, ScrapedLead

logger = logging.getLogger(__name__)

# Avito search queries for renovation
AVITO_QUERIES = [
    "ищу бригаду для ремонта",
    "нужна бригада ремонт",
    "ищу мастеров для ремонта",
    "ремонт квартиры ищу",
    "нужен ремонт квартиры",
    "поиск ремонтной бригады",
    "кто делает ремонт",
    "нужен подрядчик ремонт",
]

NOISE_PATTERNS = [
    r"продам\s+(?:квартиру|дом|участок)",
    r"куплю\s+(?:квартиру|дом|участок)",
    r"аренда\s+(?:квартиры|дома|офиса)",
    r"сдаю\s+(?:квартиру|дом|комнату)",
    r"ищу\s+работу",
    r"вакансия",
    r"резюме",
]


class AvitoScraper(BaseScraper):
    """Search Avito for renovation-related postings."""

    SOURCE_NAME = "avito"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.city = config.get("city", "moskva")
        self.base_url = f"https://www.avito.ru/{self.city}"

    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search Avito for renovation-related postings."""
        leads = []
        search_url = f"{self.base_url}/predluscheniya_uslug/remont_i_stroitelstvo"

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }
                params = {"q": query}
                async with session.get(search_url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        self.logger.warning("Avito returned status %d", resp.status)
                        return leads

                    html = await resp.text()
                    leads = self._parse_search_results(html, query)

        except Exception as e:
            self.logger.error("Avito search failed: %s", e)

        return leads[:limit]

    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor Avito for new renovation postings."""
        all_leads = []
        search_queries = queries or AVITO_QUERIES

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

        self.logger.info("Avito: found %d unique leads from %d queries", len(unique_leads), len(search_queries))
        return unique_leads

    async def search_services(self, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search in Avito services section for people looking for renovation."""
        leads = []
        search_url = f"https://www.avito.ru/{city or self.city}/predluscheniya_uslug"

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }
                params = {"q": "ищу бригаду ремонта"}
                async with session.get(search_url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return leads

                    html = await resp.text()
                    leads = self._parse_search_results(html, "services_search")

        except Exception as e:
            self.logger.error("Avito services search failed: %s", e)

        return leads[:limit]

    def _parse_search_results(self, html: str, query: str) -> list[ScrapedLead]:
        """Parse Avito search results HTML."""
        leads = []
        # Simple regex-based extraction (production would use BeautifulSoup)
        # Look for item cards with titles and descriptions
        item_pattern = re.compile(
            r'data-item-id="(\d+)".*?'
            r'(?:itemprop="name"[^>]*>([^<]+)</[^>]+>).*?'
            r'(?:itemprop="description"[^>]*>([^<]+)</[^>]+>)',
            re.DOTALL
        )

        for match in item_pattern.finditer(html):
            item_id = match.group(1)
            title = match.group(2).strip() if match.group(2) else ""
            description = match.group(3).strip() if match.group(3) else ""

            text = f"{title}\n{description}".strip()
            if not text or len(text) < 20:
                continue

            # Check if this is someone looking for renovation (not offering)
            if self._is_seeker(text):
                lead = ScrapedLead(
                    source="avito",
                    source_id=f"avito_{item_id}",
                    source_url=f"https://www.avito.ru/{self.city}/{item_id}",
                    text=text,
                    category=self._detect_category(text),
                    budget=self._estimate_budget(text),
                    urgency=self._classify_urgency(text),
                    keywords_matched=self._get_matched_keywords(text),
                    raw_data={"query": query, "title": title, "description": description},
                )
                leads.append(lead)

        return leads

    def _is_seeker(self, text: str) -> bool:
        """Check if text is from someone SEEKING renovation (not offering)."""
        text_lower = text.lower()

        # Seeker signals
        seeker_patterns = [
            r"ищу\s+(?:бригаду|мастеров|подрядчика)",
            r"нужн[аоы]\s+(?:бригада|мастер|подрядчик)",
            r"кто\s+(?:сделает|ремонтирует|делает)\s+ремонт",
            r"сколько\s+стоит\s+ремонт",
            r"ищу\s+(?:кого-то|людей)\s+для\s+ремонта",
            r"ремонт\s+(?:квартиры|дома|кухни|ванной)",
        ]

        for pattern in seeker_patterns:
            if re.search(pattern, text_lower):
                return True

        # Exclude noise
        for pattern in NOISE_PATTERNS:
            if re.search(pattern, text_lower):
                return False

        # If it mentions renovation but doesn't match noise, might be a lead
        renovation_words = ["ремонт", "отделка", "строительство", "бригада", "мастер"]
        if any(w in text_lower for w in renovation_words):
            return True

        return False

    def _get_matched_keywords(self, text: str) -> list[str]:
        """Get list of matched renovation keywords."""
        text_lower = text.lower()
        keywords = [
            "ремонт", "отделка", "бригада", "мастер", "подрядчик",
            "квартира", "кухня", "ванная", "дом", "капитальный",
            "косметический", "евроремонт", "дизайн", "плитка",
            "электрика", "сантехника", "гипсокартон",
        ]
        return [kw for kw in keywords if kw in text_lower]
