"""CIAN scraper for new apartment buyers who need renovation."""
import logging
import aiohttp
from typing import Optional
from .base import BaseScraper, ScrapedLead

logger = logging.getLogger(__name__)


class CIANScraper(BaseScraper):
    """Monitor CIAN for new property buyers who might need renovation."""

    SOURCE_NAME = "cian"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.api_key = config.get("cian_api_key", "")
        self.city = config.get("city", "moscow")

    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search CIAN for properties that might need renovation."""
        leads = []

        # CIAN API for new buildings (novostroyki)
        # People buying new builds often need renovation
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "LeadHunter/1.0",
                    "Accept": "application/json",
                }

                # Search for new buildings without finishing (чистовая отделка)
                params = {
                    "city": city or self.city,
                    "type": "newbuilding",
                    "finishing": "no",  # No finishing = needs renovation
                    "limit": min(limit, 100),
                }

                if self.api_key:
                    headers["Authorization"] = f"Token {self.api_key}"

                async with session.get(
                    "https://api.cian.ru/search-offers/v2/search-offers-desktop/",
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        leads = self._parse_cian_response(data)
                    else:
                        self.logger.warning("CIAN API returned status %d", resp.status)

        except Exception as e:
            self.logger.error("CIAN search failed: %s", e)

        return leads[:limit]

    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor CIAN for new properties without finishing."""
        all_leads = []

        # Focus on new buildings without finishing
        leads = await self.search("newbuilding", city=self.city)
        all_leads.extend(leads)

        # Also search for apartments with old finishing (likely need renovation)
        leads2 = await self.search("old_house", city=self.city)
        all_leads.extend(leads2)

        # Deduplicate
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.source_id not in seen:
                seen.add(lead.source_id)
                unique_leads.append(lead)

        self.logger.info("CIAN: found %d unique leads", len(unique_leads))
        return unique_leads

    async def search_newbuilds_without_finishing(self, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search for new buildings without finishing (perfect renovation leads)."""
        leads = []
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "LeadHunter/1.0",
                    "Accept": "application/json",
                }

                params = {
                    "city": city or self.city,
                    "type": "newbuilding",
                    "finishing": "no",
                    "limit": min(limit, 100),
                }

                async with session.get(
                    "https://api.cian.ru/search-offers/v2/search-offers-desktop/",
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        leads = self._parse_newbuild_response(data)

        except Exception as e:
            self.logger.error("CIAN newbuild search failed: %s", e)

        return leads[:limit]

    def _parse_cian_response(self, data: dict) -> list[ScrapedLead]:
        """Parse CIAN API response."""
        leads = []
        items = data.get("data", {}).get("offers", [])

        for item in items:
            lead = self._parse_cian_offer(item)
            if lead:
                leads.append(lead)

        return leads

    def _parse_newbuild_response(self, data: dict) -> list[ScrapedLead]:
        """Parse newbuild response into renovation leads."""
        leads = []
        items = data.get("data", {}).get("offers", [])

        for item in items:
            # People buying new builds without finishing = renovation opportunity
            lead = self._create_renovation_lead_from_property(item)
            if lead:
                leads.append(lead)

        return leads

    def _parse_cian_offer(self, offer: dict) -> Optional[ScrapedLead]:
        """Parse a CIAN offer into ScrapedLead."""
        try:
            offer_id = str(offer.get("id", ""))
            title = offer.get("title", "")
            description = offer.get("description", "")
            price = offer.get("price", {})
            url = offer.get("url", "")

            # Get property details
            rooms = offer.get("roomsCount", "")
            area = offer.get("area", {})
            district = offer.get("district", {}).get("name", "")
            address = offer.get("address", "")

            text = f"{title}\n{description}\n\nРайон: {district}\nАдрес: {address}\nКомнат: {rooms}\nПлощадь: {area.get('value', '')} м²"

            return ScrapedLead(
                source="cian",
                source_id=f"cian_{offer_id}",
                source_url=url or f"https://www.cian.ru/buy-{offer_id}/",
                text=text,
                city=district or self.city,
                category="apartment",
                budget=self._extract_price(price),
                urgency="medium",  # New buyers usually plan renovation soon
                keywords_matched=["новостройка", "без отделки", "ремонт"],
                raw_data=offer,
            )
        except Exception as e:
            self.logger.error("Failed to parse CIAN offer: %s", e)
            return None

    def _create_renovation_lead_from_property(self, property_data: dict) -> Optional[ScrapedLead]:
        """Create a renovation lead from property listing."""
        try:
            prop_id = str(property_data.get("id", ""))
            address = property_data.get("address", "")
            price = property_data.get("price", {})
            rooms = property_data.get("roomsCount", "")
            area = property_data.get("area", {}).get("value", "")
            finishing = property_data.get("finishing", "")

            # If property has no finishing or old finishing, it's a renovation opportunity
            if finishing and finishing not in ["none", "no", "cosmetic"]:
                return None

            text = (
                f"Новая покупка квартиры (возможно нужен ремонт)\n"
                f"Адрес: {address}\n"
                f"Комнат: {rooms}\n"
                f"Площадь: {area} м²\n"
                f"Отделка: {finishing or 'без отделки'}"
            )

            return ScrapedLead(
                source="cian",
                source_id=f"cian_new_{prop_id}",
                source_url=f"https://www.cian.ru/buy-{prop_id}/",
                text=text,
                city=self.city,
                category="apartment",
                budget=self._extract_price(price),
                urgency="high" if not finishing or finishing in ["none", "no"] else "medium",
                keywords_matched=["новостройка", "без отделки", "покупка квартиры"],
                raw_data=property_data,
            )
        except Exception as e:
            self.logger.error("Failed to create renovation lead: %s", e)
            return None

    def _extract_price(self, price_data: dict) -> Optional[float]:
        """Extract price from CIAN price data."""
        try:
            if isinstance(price_data, dict):
                return float(price_data.get("value", 0))
            return float(price_data) if price_data else None
        except (ValueError, TypeError):
            return None
