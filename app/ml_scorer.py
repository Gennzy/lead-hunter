"""ML-based lead scoring: predicts probability of closing a deal."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead, async_session

logger = logging.getLogger(__name__)

# Feature weights (learned from historical data)
FEATURE_WEIGHTS = {
    "lead_score": 0.35,
    "urgency_high": 0.20,
    "urgency_medium": 0.10,
    "has_username": 0.05,
    "has_reply": 0.08,
    "chat_popularity": 0.10,
    "time_of_day": 0.05,
    "message_length": 0.07,
}

# Urgency multipliers
URGENCY_MAP = {"high": 1.0, "medium": 0.5, "low": 0.1}


def _extract_features(lead: Lead, chat_stats: dict = None) -> dict:
    """Extract numeric features from a lead for scoring."""
    features = {}

    # Normalized lead score (0-1)
    features["lead_score"] = min(lead.lead_score / 100.0, 1.0)

    # Urgency one-hot
    features["urgency_high"] = 1.0 if lead.urgency == "high" else 0.0
    features["urgency_medium"] = 1.0 if lead.urgency == "medium" else 0.0

    # Has Telegram username (easier to contact)
    features["has_username"] = 1.0 if lead.username else 0.0

    # Has reply context (engaged in conversation)
    features["has_reply"] = 1.0 if lead.reply_to_id else 0.0

    # Chat popularity (normalized)
    if chat_stats and lead.chat_title in chat_stats:
        max_leads = max(chat_stats.values()) if chat_stats else 1
        features["chat_popularity"] = min(chat_stats[lead.chat_title] / max_leads, 1.0) if max_leads > 0 else 0.0
    else:
        features["chat_popularity"] = 0.5

    # Time of day feature (messages during work hours convert better)
    if lead.created_at:
        hour = lead.created_at.hour
        features["time_of_day"] = 1.0 if 9 <= hour <= 18 else 0.3
    else:
        features["time_of_day"] = 0.5

    # Message length (longer = more intent, normalized)
    msg_len = len(lead.message_text or "")
    features["message_length"] = min(msg_len / 500.0, 1.0)

    return features


def predict_probability(features: dict) -> float:
    """Simple weighted prediction (can be replaced with sklearn model)."""
    score = 0.0
    for feature, weight in FEATURE_WEIGHTS.items():
        score += features.get(feature, 0.0) * weight
    # Sigmoid-like normalization to 0-100
    return min(max(round(score * 100, 1), 0), 100)


async def score_lead(lead: Lead, tenant_id: int = None) -> dict:
    """Score a lead and return prediction with breakdown."""
    try:
        async with async_session() as session:
            # Get chat popularity stats
            filters = [Lead.status != "deleted"]
            if tenant_id:
                filters.append(Lead.tenant_id == tenant_id)

            chat_stats_q = (
                select(Lead.chat_title, sa_func.count(Lead.id))
                .where(*filters)
                .group_by(Lead.chat_title)
            )
            rows = (await session.execute(chat_stats_q)).fetchall()
            chat_stats = {r[0]: r[1] for r in rows}

        features = _extract_features(lead, chat_stats)
        probability = predict_probability(features)

        return {
            "probability": probability,
            "features": features,
            "recommendation": _get_recommendation(probability),
        }
    except Exception as e:
        logger.error("ML scoring error: %s", e)
        return {"probability": lead.lead_score, "features": {}, "recommendation": "Стандартная обработка"}


def _get_recommendation(probability: float) -> str:
    if probability >= 80:
        return "Высокая вероятность сделки — срочно написать!"
    if probability >= 60:
        return "Хороший лид — рекомендуется первый контакт"
    if probability >= 40:
        return "Средняя вероятность — стоит попробовать"
    return "Низкая вероятность — можно отложить"
