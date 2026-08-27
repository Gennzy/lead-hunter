"""Russian city mappings for all scrapers.

Each scraper needs cities in different formats:
- Avito: URL slug (e.g., "moskva", "sankt-peterburg")
- CIAN: region ID (e.g., 1 for Moscow, 2 for SPB)
- VK: city ID (VK database city ID)
- ForumHouse: no city needed (national forum)

This module provides a unified mapping from Russian city names
to all required formats.
"""

# Main mapping: Russian name -> {avito, cian, vk_id}
CITY_MAP = {
    "москва": {"avito": "moskva", "cian": 1, "vk_id": 1},
    "санкт-петербург": {"avito": "sankt-peterburg", "cian": 2, "vk_id": 2},
    "петербург": {"avito": "sankt-peterburg", "cian": 2, "vk_id": 2},
    "питер": {"avito": "sankt-peterburg", "cian": 2, "vk_id": 2},
    "спб": {"avito": "sankt-peterburg", "cian": 2, "vk_id": 2},
    "казань": {"avito": "kazan", "cian": 4774, "vk_id": 43},
    "новосибирск": {"avito": "novosibirsk", "cian": 4897, "vk_id": 65},
    "екатеринбург": {"avito": "ekaterinburg", "cian": 5036, "vk_id": 3},
    "нижний новгород": {"avito": "nizhniy_novgorod", "cian": 4890, "vk_id": 48},
    "челябинск": {"avito": "chelyabinsk", "cian": 5061, "vk_id": 56},
    "самара": {"avito": "samara", "cian": 4956, "vk_id": 78},
    "уфа": {"avito": "ufa", "cian": 5073, "vk_id": 73},
    "красноярск": {"avito": "krasnoyarsk", "cian": 4882, "vk_id": 54},
    "воронеж": {"avito": "voronezh", "cian": 4703, "vk_id": 22},
    "пермь": {"avito": "perm", "cian": 4908, "vk_id": 70},
    "волгоград": {"avito": "volgograd", "cian": 4695, "vk_id": 16},
    "краснодар": {"avito": "krasnodar", "cian": 4761, "vk_id": 53},
    "ростов-на-дону": {"avito": "rostov-na-donu", "cian": 4960, "vk_id": 76},
    "ростов": {"avito": "rostov-na-donu", "cian": 4960, "vk_id": 76},
    "омск": {"avito": "omsk", "cian": 4900, "vk_id": 66},
    "барнаул": {"avito": "barnaul", "cian": 4710, "vk_id": 5},
    "иркутск": {"avito": "irkutsk", "cian": 4737, "vk_id": 35},
    "тюмень": {"avito": "tyumen", "cian": 5051, "vk_id": 71},
    "Аханка": {"avito": "habarovsk", "cian": 4708, "vk_id": 4},
    "владивосток": {"avito": "vladivostok", "cian": 4507, "vk_id": 15},
    "махачкала": {"avito": "mahachkala", "cian": 4759, "vk_id": 86},
    "томск": {"avito": "tomsk", "cian": 5013, "vk_id": 69},
    "оренбург": {"avito": "orenburg", "cian": 4903, "vk_id": 68},
    "кемерово": {"avito": "kemerovo", "cian": 4848, "vk_id": 42},
    "рязань": {"avito": "ryazan", "cian": 4935, "vk_id": 77},
    "набережные челны": {"avito": "naberezhnye_chelny", "cian": 4872, "vk_id": 49},
    "саратов": {"avito": "saratov", "cian": 4938, "vk_id": 79},
    "Сальск": {"avito": "izhevsk", "cian": 4738, "vk_id": 36},
    "тольятти": {"avito": "tolyatti", "cian": 5010, "vk_id": 68},
    "ижевск": {"avito": "izhevsk", "cian": 4738, "vk_id": 36},
    "барнауль": {"avito": "barnaul", "cian": 4710, "vk_id": 5},
    "хабаровск": {"avito": "habarovsk", "cian": 4708, "vk_id": 4},
    "ulan-ude": {"avito": "ulan-ude", "cian": 5043, "vk_id": 72},
    "уладь-удэ": {"avito": "ulan-ude", "cian": 5043, "vk_id": 72},
    "vladivostok": {"avito": "vladivostok", "cian": 4507, "vk_id": 15},
    "khabarovsk": {"avito": "habarovsk", "cian": 4708, "vk_id": 4},
}

# Default renovation queries per source (used when user doesn't specify)
DEFAULT_QUERIES = {
    "vk": [
        "ищу бригаду для ремонта",
        "нужна бригада ремонт",
        "ремонт квартиры ищу",
        "капитальный ремонт",
        "отделочные работы",
        "строительная бригада",
    ],
    "avito": [
        "ищу бригаду для ремонта",
        "нужна бригада ремонт",
        "ремонт квартиры",
        "ищу мастеров для ремонта",
    ],
    "cian": [],  # CIAN doesn't use text queries
    "forumhouse": [
        "ищу бригаду для ремонта",
        "нужна бригада ремонт",
        "ремонт квартиры",
    ],
}

# Default ForumHouse sections
DEFAULT_FORUMHOUSE_SECTIONS = [
    "remont-i-otdelka",
    "stroitelstvo",
    "inzhenernye-sistemy",
    "interier",
]


def resolve_city(russian_name: str) -> dict | None:
    """Resolve a Russian city name to all scraper formats.

    Returns dict with keys: avito, cian, vk_id
    Or None if city not found.
    """
    key = russian_name.strip().lower()
    return CITY_MAP.get(key)


def get_avito_slug(russian_name: str) -> str:
    """Get Avito URL slug from Russian city name."""
    info = resolve_city(russian_name)
    if info:
        return info["avito"]
    # Fallback: transliterate
    return russian_name.strip().lower().replace(" ", "_").replace("-", "_")


def get_cian_region_id(russian_name: str) -> int:
    """Get CIAN region ID from Russian city name."""
    info = resolve_city(russian_name)
    if info:
        return info["cian"]
    return 2  # Default to SPB


def get_vk_city_id(russian_name: str) -> int | None:
    """Get VK city ID from Russian city name."""
    info = resolve_city(russian_name)
    if info:
        return info.get("vk_id")
    return None


def list_cities() -> list[str]:
    """Return sorted list of supported Russian city names."""
    return sorted(CITY_MAP.keys())
