"""Multi-channel lead scrapers for construction/renovation company."""
from .base import BaseScraper, ScrapedLead
from .vk_scraper import VKScraper
from .avito_scraper import AvitoScraper
from .cian_scraper import CIANScraper
from .forumhouse_scraper import ForumHouseScraper

__all__ = [
    "BaseScraper", "ScrapedLead",
    "VKScraper", "AvitoScraper", "CIANScraper", "ForumHouseScraper",
]
