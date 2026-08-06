"""VK group monitoring for renovation leads."""
import logging
import aiohttp
from typing import Optional
from .base import BaseScraper, ScrapedLead

logger = logging.getLogger(__name__)

# Renovation-related keywords for VK search
RENOVATION_KEYWORDS = [
    "ищу бригаду", "нужна бригада", "ищу мастеров", "нужен мастер",
    "ремонт квартиры", "ремонт комнаты", "ремонт кухни", "ремонт ванной",
    "отделка квартиры", "отделочные работы", "строительная бригада",
    "капитальный ремонт", "косметический ремонт", "евроремонт",
    "дизайн интерьера", "дизайн проект", "перепланировка",
    "электрик", "сантехник", "плиточник", "маляр", "штукатур",
    "смета на ремонт", "стоимость ремонта", "цена ремонта",
    "гипсокартон", "стяжка пола", "утепление", "звукоизоляция",
]

NOISE_KEYWORDS = [
    "продам", "куплю", "аренда", "сдаю", "ищу работу",
    "вакансия", "резюме", "фото ремонта", "наш ремонт",
    "хвастаюсь", "реклама", "скидка 50", "акция有限",
]


class VKScraper(BaseScraper):
    """Monitor VK groups for renovation leads."""

    SOURCE_NAME = "vk"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.access_token = config.get("vk_access_token", "")
        self.api_version = "5.199"

    async def _api_call(self, method: str, params: dict = None) -> dict:
        """Make VK API call."""
        if not self.access_token:
            self.logger.warning("VK access token not configured")
            return {}

        url = f"https://api.vk.com/method/{method}"
        data = {
            "access_token": self.access_token,
            "v": self.api_version,
            **(params or {}),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    result = await resp.json()
                    if "error" in result:
                        self.logger.error("VK API error: %s", result["error"])
                        return {}
                    return result.get("response", {})
        except Exception as e:
            self.logger.error("VK API request failed: %s", e)
            return {}

    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search VK wall posts by query."""
        params = {
            "q": query,
            "count": min(limit, 100),
            "extended": 1,
        }
        if city:
            params["city"] = await self._get_city_id(city)

        result = await self._api_call("newsfeed.search", params)
        leads = []

        for item in result.get("items", []):
            lead = self._parse_wall_post(item)
            if lead and self._matches_keywords(lead.text):
                leads.append(lead)

        return leads

    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor VK for new posts matching renovation queries."""
        all_leads = []
        search_queries = queries or RENOVATION_KEYWORDS[:10]

        for query in search_queries:
            leads = await self.search(query, city=cities[0] if cities else "")
            all_leads.extend(leads)

        # Deduplicate by source_id
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.source_id not in seen:
                seen.add(lead.source_id)
                unique_leads.append(lead)

        self.logger.info("VK: found %d unique leads from %d queries", len(unique_leads), len(search_queries))
        return unique_leads

    async def search_group(self, owner_id: str, count: int = 100) -> list[ScrapedLead]:
        """Search posts in a specific VK group."""
        params = {
            "owner_id": owner_id if owner_id.startswith("-") else f"-{owner_id}",
            "count": min(count, 100),
            "extended": 0,
        }
        result = await self._api_call("wall.get", params)
        leads = []

        for item in result.get("items", []):
            lead = self._parse_wall_post(item)
            if lead and self._matches_keywords(lead.text):
                leads.append(lead)

        return leads

    async def monitor_groups(self, group_ids: list[str], limit_per_group: int = 50) -> list[ScrapedLead]:
        """Monitor specific VK groups for renovation posts."""
        all_leads = []
        for group_id in group_ids:
            leads = await self.search_group(group_id, count=limit_per_group)
            all_leads.extend(leads)
            self.logger.info("VK group %s: found %d leads", group_id, len(leads))

        # Deduplicate
        seen = set()
        unique_leads = []
        for lead in all_leads:
            if lead.source_id not in seen:
                seen.add(lead.source_id)
                unique_leads.append(lead)

        return unique_leads

    def _parse_wall_post(self, item: dict) -> Optional[ScrapedLead]:
        """Parse a VK wall post into ScrapedLead."""
        try:
            text = item.get("text", "")
            if not text or len(text) < 20:
                return None

            post_id = str(item.get("id", ""))
            owner_id = item.get("owner_id", "")
            source_url = f"https://vk.com/wall{owner_id}_{post_id}"

            author_name = ""
            author_username = ""
            if "from_id" in item:
                author_id = item["from_id"]
                if author_id > 0:
                    author_name = f"User {author_id}"

            return ScrapedLead(
                source="vk",
                source_id=f"vk_{owner_id}_{post_id}",
                source_url=source_url,
                text=text,
                author_name=author_name,
                author_username=author_username,
                author_id=str(item.get("from_id", "")),
                category=self._detect_category(text),
                budget=self._estimate_budget(text),
                urgency=self._classify_urgency(text),
                keywords_matched=self._get_matched_keywords(text),
                raw_data=item,
            )
        except Exception as e:
            self.logger.error("Failed to parse VK post: %s", e)
            return None

    def _matches_keywords(self, text: str) -> bool:
        """Check if text matches renovation keywords."""
        text_lower = text.lower()
        # Must match at least one renovation keyword
        if not any(kw in text_lower for kw in RENOVATION_KEYWORDS):
            return False
        # Must not be noise
        if any(kw in text_lower for kw in NOISE_KEYWORDS):
            return False
        return True

    def _get_matched_keywords(self, text: str) -> list[str]:
        """Get list of matched keywords."""
        text_lower = text.lower()
        return [kw for kw in RENOVATION_KEYWORDS if kw in text_lower]

    async def _get_city_id(self, city_name: str) -> Optional[int]:
        """Get VK city ID by name."""
        result = await self._api_call("database.getCities", {"q": city_name, "count": 1})
        cities = result.get("items", [])
        if cities:
            return cities[0].get("id")
        return None
