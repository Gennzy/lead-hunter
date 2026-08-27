"""Avito scraper for renovation leads.

Uses curl_cffi for TLS fingerprint impersonation to bypass Cloudflare.
Supports optional Russian residential proxy for better success rate.
"""
import logging
import re
import random
import asyncio
from typing import Optional
from .base import BaseScraper, ScrapedLead, retry_on_error

logger = logging.getLogger(__name__)

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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


class AvitoScraper(BaseScraper):
    """Search Avito for renovation-related postings.

    Uses curl_cffi for TLS fingerprint impersonation.
    Optionally supports HTTP proxy for Russian residential IPs.
    """

    SOURCE_NAME = "avito"

    def __init__(self, config: dict = None):
        super().__init__(config)
        from .cities import get_avito_slug
        raw_city = config.get("city", "Москва")
        self.city = get_avito_slug(raw_city)
        self.city_display = raw_city
        self.base_url = f"https://www.avito.ru/{self.city}"
        # Optional proxy: "http://user:pass@host:port"
        self.proxy = config.get("proxy")

    def _get_session(self):
        """Create a curl_cffi session with browser TLS fingerprint."""
        try:
            from curl_cffi import requests as cffi_requests
            session = cffi_requests.Session(impersonate="chrome131")
            return session, True
        except ImportError:
            return None, False

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }

    @retry_on_error(max_retries=2, backoff=5)
    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search Avito for renovation-related postings."""
        leads = []
        search_url = f"{self.base_url}/predlozheniya_uslug/remont_i_stroitelstvo"

        session, has_cffi = self._get_session()
        if not has_cffi:
            self.logger.warning("curl_cffi not available, falling back to aiohttp")
            return await self._search_fallback(query, limit)

        try:
            proxies = {"https": self.proxy, "http": self.proxy} if self.proxy else None
            headers = self._get_headers()

            # Random delay to avoid detection
            await asyncio.sleep(random.uniform(1.5, 3.5))

            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.get(
                    search_url,
                    params={"q": query},
                    headers=headers,
                    proxies=proxies,
                    timeout=30,
                ),
            )

            if response.status_code == 404:
                self.logger.warning("Avito returned 404 for query: %s", query)
                return leads

            if response.status_code == 429:
                self.logger.warning("Avito rate limited (429)")
                self.log_error("Rate limited (429)")
                return leads

            if response.status_code != 200:
                self.logger.warning("Avito returned status %d", response.status_code)
                return leads

            html = response.text
            leads = self._parse_search_results(html, query)

        except Exception as e:
            self.logger.error("Avito search failed: %s", e)
            self.log_error(str(e))

        return leads[:limit]

    async def _search_fallback(self, query: str, limit: int) -> list[ScrapedLead]:
        """Fallback to aiohttp if curl_cffi is not available."""
        import aiohttp

        leads = []
        search_url = f"{self.base_url}/predlozheniya_uslug/remont_i_stroitelstvo"

        try:
            async with aiohttp.ClientSession() as session:
                headers = self._get_headers()
                await asyncio.sleep(random.uniform(2.0, 4.0))
                async with session.get(
                    search_url,
                    headers=headers,
                    params={"q": query},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        self.logger.warning("Avito fallback returned status %d", resp.status)
                        return leads
                    html = await resp.text()
                    leads = self._parse_search_results(html, query)
        except Exception as e:
            self.logger.error("Avito fallback failed: %s", e)

        return leads[:limit]

    @retry_on_error(max_retries=2, backoff=5)
    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor Avito for new renovation postings."""
        all_leads = []
        search_queries = queries or AVITO_QUERIES

        for query in search_queries:
            leads = await self.search(query)
            all_leads.extend(leads)
            # Delay between queries
            await asyncio.sleep(random.uniform(2.0, 4.0))

        # Deduplicate
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.source_id not in seen:
                seen.add(lead.source_id)
                unique_leads.append(lead)

        self.logger.info("Avito: found %d unique leads from %d queries", len(unique_leads), len(search_queries))
        return unique_leads

    def _parse_search_results(self, html: str, query: str) -> list[ScrapedLead]:
        """Parse Avito search results HTML."""
        leads = []

        # Multiple patterns for different Avito HTML versions
        patterns = [
            # Pattern 1: data-item-id with itemprop
            re.compile(
                r'data-item-id="(\d+)".*?'
                r'(?:itemprop="name"[^>]*>([^<]+)</[^>]+>).*?'
                r'(?:itemprop="description"[^>]*>([^<]+)</[^>]+>)',
                re.DOTALL,
            ),
            # Pattern 2:iva-item with title and snippet
            re.compile(
                r'iva-item[^"]*"[^>]*data-item-id="(\d+)".*?'
                r'(?:item-title[^>]*>.*?<span[^>]*>([^<]+)</span>).*?'
                r'(?:item-snippet[^>]*>.*?<span[^>]*>([^<]+)</span>)',
                re.DOTALL,
            ),
            # Pattern 3: simpler extraction
            re.compile(
                r'href="/[^"]*?/(\d{10,})"[^>]*>.*?'
                r'<[^>]*>([^<]{20,200})</[^>]*>',
                re.DOTALL,
            ),
        ]

        for pattern in patterns:
            for match in pattern.finditer(html):
                item_id = match.group(1)
                title = match.group(2).strip() if match.lastindex >= 2 and match.group(2) else ""
                description = match.group(3).strip() if match.lastindex >= 3 and match.group(3) else ""

                text = f"{title}\n{description}".strip() if description else title
                if not text or len(text) < 15:
                    continue

                # Skip duplicates within same parse
                source_id = f"avito_{item_id}"
                if any(l.source_id == source_id for l in leads):
                    continue

                if self._is_seeker(text):
                    lead = ScrapedLead(
                        source="avito",
                        source_id=source_id,
                        source_url=f"https://www.avito.ru/{self.city}/{item_id}",
                        text=text[:2000],
                        city=self.city_display,
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

        seeker_patterns = [
            r"ищу\s+(?:бригаду|мастеров|подрядчика)",
            r"нужн[аоы]\s+(?:бригада|мастер|подрядчик)",
            r"кто\s+(?:сделает|ремонтирует|делает)\s+ремонт",
            r"сколько\s+стоит\s+ремонт",
            r"ищу\s+(?:кого-то|людей)\s+для\s+ремонта",
            r"ремонт\s+(?:квартиры|дома|кухни|ванной)",
            r"планиру[ею]\s+ремонт",
            r"хочу\s+сделать\s+ремонт",
            r"нужен\s+(?:ремонт|отделка)",
        ]

        for pattern in seeker_patterns:
            if re.search(pattern, text_lower):
                return True

        for pattern in NOISE_PATTERNS:
            if re.search(pattern, text_lower):
                return False

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
