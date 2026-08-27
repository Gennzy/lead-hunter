"""CIAN scraper for new apartment buyers who need renovation."""
import logging
import re
import aiohttp
from typing import Optional
from .base import BaseScraper, ScrapedLead, retry_on_error

logger = logging.getLogger(__name__)


class CIANScraper(BaseScraper):
    """Monitor CIAN for new property buyers who might need renovation."""

    SOURCE_NAME = "cian"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.city = config.get("city", "Санкт-Петербург")

    @retry_on_error(max_retries=2, backoff=3)
    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search CIAN for properties that might need renovation."""
        leads = []
        target_city = city or self.city

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }

                # Search for newbuilds without finishing
                url = f"https://www.cian.ru/cat.php?deal_type=sale&engine_version=2&newobject%5B0%5D=1&offer_type=flat&p=1&region={self._get_region_id(target_city)}&room1=0&room2=1&room3=1&room4=1&without_finishing=1"

                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        leads = self._parse_html(html)
                    else:
                        self.logger.warning("CIAN returned status %d", resp.status)

        except Exception as e:
            self.logger.error("CIAN search failed: %s", e)
            self.log_error(str(e))

        return leads[:limit]

    @retry_on_error(max_retries=2, backoff=3)
    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor CIAN for new properties without finishing."""
        target_city = (cities or [self.city])[0]
        leads = await self.search("newbuild", city=target_city)

        self.logger.info("CIAN: found %d leads", len(leads))
        return leads

    def _parse_html(self, html: str) -> list[ScrapedLead]:
        """Parse CIAN search results from HTML."""
        leads = []

        # Extract offer cards from CIAN HTML
        # CIAN uses data-name="CardsSerpItem" for offer cards
        card_pattern = re.compile(
            r'data-name="CardsSerpItem"[^>]*>(.*?)</div>\s*</div>\s*</div>',
            re.DOTALL | re.IGNORECASE
        )

        # Also try extracting from JSON-LD or script tags
        json_pattern = re.compile(r'"offerCard":\s*(\{.*?\})\s*[,}]', re.DOTALL)

        # Fallback: extract links and titles
        link_pattern = re.compile(
            r'href="(https://www\.cian\.ru/buy[^"]*)"[^>]*>.*?<[^>]*>([^<]{10,})</[^>]*>',
            re.DOTALL | re.IGNORECASE
        )

        for match in link_pattern.finditer(html):
            url = match.group(1)
            title = match.group(2).strip()

            if not title or len(title) < 10:
                continue

            # Extract offer ID from URL
            id_match = re.search(r'/(\d+)/', url)
            offer_id = id_match.group(1) if id_match else str(hash(url))

            # Check if this is a property without finishing
            text_lower = title.lower()
            if any(w in text_lower for w in ["без отделки", "чистовая", "предчистовая", "white box"]):
                urgency = "high"
            elif any(w in text_lower for w in ["новостройк", "новый дом", "сдача"]):
                urgency = "medium"
            else:
                urgency = "low"

            lead = ScrapedLead(
                source="cian",
                source_id=f"cian_{offer_id}",
                source_url=url,
                text=title,
                city=self.city,
                category="apartment",
                urgency=urgency,
                keywords_matched=["новостройка", "без отделки"],
                raw_data={"title": title},
            )
            leads.append(lead)

        return leads

    def _get_region_id(self, city: str) -> int:
        """Get CIAN region ID by city name (supports Russian names)."""
        from .cities import get_cian_region_id, CITY_MAP

        # Try Russian name first
        region = get_cian_region_id(city)
        if region:
            return region

        # Fallback to English slug
        regions = {
            "sankt-peterburg": 2, "moskva": 1, "moscow": 1, "spb": 2,
            "kazan": 4774, "novosibirsk": 4897, "ekaterinburg": 5036,
            "nizhniy_novgorod": 4890, "chelyabinsk": 5061, "samara": 4956,
            "ufa": 5073, "krasnoyarsk": 4882, "voronezh": 4703,
            "perm": 4908, "volgograd": 4695,
        }
        return regions.get(city.lower(), 2)
