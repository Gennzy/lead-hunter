"""VK group monitoring for renovation leads.

Improved filtering: detects people SEEKING renovation (not offering).
Focuses on personal posts in renovation groups.
"""
import logging
import re
import aiohttp
from typing import Optional
from .base import BaseScraper, ScrapedLead, retry_on_error

logger = logging.getLogger(__name__)

# Intent patterns — person is LOOKING for renovation crew
INTENT_PATTERNS = [
    r"ищу\s+(?:бригаду|мастеров|подрядчика|строительную\s+компанию)",
    r"нужн[аоы]\s+(?:бригада|мастер|подрядчик|стройотряд)",
    r"кто\s+(?:сделает|ремонтирует|выполнит|сделает)\s+ремонт",
    r"ищу\s+кого[- ]?то\s+для\s+ремонта",
    r"подскажите\s+(?:бригаду|мастеров|кого)\s+для\s+ремонта",
    r"посоветуйте\s+(?:бригаду|мастеров|подрядчика)",
    r"рекомендуйте\s+(?:бригаду|мастеров)",
    r"сколько\s+стоит\s+ремонт",
    r"хочу\s+сделать\s+ремонт",
    r"планиру[ею]\s+ремонт",
    r"нужен\s+ремонт\s+(?:квартиры|дома|кухни|ванной)",
    r"ищу\s+кто\s+сделает\s+ремонт",
    r"ищу\s+людей\s+для\s+ремонта",
    r"кто\s+можем?\s+сделать",
    r"нужны\s+работники",
    r"ищу\s+рабочих",
]

# Noise — news, articles, ads, offers, general talk
NOISE_PATTERNS = [
    # Ads / services offered
    r"(?:дела[ею]|выполн[яю]|предлага[ею]|оказыва[ею])\s+(?:ремонт|отделк)",
    r"(?:наш[аи]?\s+компания|наша\s+бригада|мы\s+делаем)",
    r"скидк[аи]\s+\d+",
    r"акци[яию]\s+",
    r"бесплатн\w+\s+(?:замер|расчёт|консультац)",
    r"гаранти[яию]\s+на\s+ремонт",
    r"(?:portfolio|портфолио|наши\s+работы)",
    # News / articles / general
    r"день\s+строител",
    r"професси[ае]льн\w+\s+праздник",
    r"важно\s+проанализиров",
    r"точки\s+касания",
    r"скрытое\s+напряжение",
    r"почему\s+\w+\s+мешал",
    r"истори[яию]\s+ремонта",
    r"обзор\s+ремонта",
    r"фото\s+ремонта",
    r"результат\w*\s+ремонта",
    r"до\s+и\s+после\s+ремонта",
    # Marketplace
    r"продам", r"куплю", r"аренда", r"сдаю",
    r"ищу\s+работу", r"вакансия", r"резюме",
    r"tarif", r"тариф", r"подписк",
]

# Groups focused on renovation (people ask for crew recommendations)
FOCUS_QUERIES = [
    "ищу бригаду для ремонта",
    "нужна бригада ремонт",
    "кто сделает ремонт",
    "ищу мастеров для ремонта",
    "посоветуйте бригаду",
    "нужен подрядчик ремонт",
]


class VKScraper(BaseScraper):
    """Monitor VK for people seeking renovation crews."""

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
            self.log_error(str(e))
            return {}

    @retry_on_error(max_retries=2, backoff=3)
    async def search(self, query: str, city: str = "", limit: int = 50) -> list[ScrapedLead]:
        """Search VK wall posts — only people SEEKING renovation."""
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
            if lead and self._is_seeker_post(lead.text):
                leads.append(lead)

        return leads

    @retry_on_error(max_retries=2, backoff=3)
    async def monitor(self, queries: list[str], cities: list[str] = None) -> list[ScrapedLead]:
        """Monitor VK for people seeking renovation."""
        all_leads = []
        search_queries = queries or FOCUS_QUERIES

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
            author_id = item.get("from_id", 0)
            if author_id > 0:
                author_name = f"User {author_id}"

            return ScrapedLead(
                source="vk",
                source_id=f"vk_{owner_id}_{post_id}",
                source_url=source_url,
                text=text[:2000],
                author_name=author_name,
                author_username=author_username,
                author_id=str(author_id),
                category=self._detect_category(text),
                budget=self._estimate_budget(text),
                urgency=self._classify_urgency(text),
                keywords_matched=self._get_matched_keywords(text),
                raw_data={"post_id": post_id, "owner_id": owner_id, "from_id": author_id},
            )
        except Exception as e:
            self.logger.error("Failed to parse VK post: %s", e)
            return None

    def _is_seeker_post(self, text: str) -> bool:
        """Check if post is from someone SEEKING renovation (not offering/news)."""
        text_lower = text.lower()

        # Must match intent pattern
        has_intent = any(re.search(p, text_lower) for p in INTENT_PATTERNS)
        if not has_intent:
            return False

        # Must not be noise
        if any(re.search(p, text_lower) for p in NOISE_PATTERNS):
            return False

        return True

    def _get_matched_keywords(self, text: str) -> list[str]:
        """Get list of matched intent keywords."""
        text_lower = text.lower()
        matched = []
        for p in INTENT_PATTERNS:
            if re.search(p, text_lower):
                matched.append(p[:30])
        return matched

    async def _get_city_id(self, city_name: str) -> Optional[int]:
        """Get VK city ID by name."""
        result = await self._api_call("database.getCities", {"q": city_name, "count": 1})
        cities = result.get("items", [])
        if cities:
            return cities[0].get("id")
        return None
