"""ML-based lead scoring with real-time learning from outcomes."""
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead, async_session
from config import utcnow

logger = logging.getLogger(__name__)

WEIGHTS_FILE = Path(__file__).parent.parent / "ml_weights.json"

FEATURE_NAMES = [
    "lead_score",
    "urgency_high",
    "urgency_medium",
    "has_username",
    "has_reply",
    "chat_popularity",
    "time_of_day",
    "message_length",
    "has_profile_link",
    "msg_exclamations",
    "msg_questions",
    "msg_word_count",
    "is_reply_to_question",
    "chat_lead_count",
    "urgency_low",
    "is_weekday",
    "has_phone",
    "has_price",
    "is_question",
    "has_renovation_keyword",
    "has_urgency_words",
    "hotness_hot",
    "hotness_warm",
    "budget_estimate",
    "budget_high",
    "timeline_asap",
    "timeline_1_3_months",
    "readiness_ready",
    "readiness_planning",
    "has_ai_summary",
    "has_context_history",
]

DEFAULT_WEIGHTS = {
    "lead_score": 0.25,
    "urgency_high": 0.12,
    "urgency_medium": 0.06,
    "has_username": 0.04,
    "has_reply": 0.05,
    "chat_popularity": 0.06,
    "time_of_day": 0.03,
    "message_length": 0.04,
    "has_profile_link": 0.02,
    "msg_exclamations": 0.02,
    "msg_questions": 0.02,
    "msg_word_count": 0.02,
    "is_reply_to_question": 0.02,
    "chat_lead_count": 0.01,
    "urgency_low": 0.02,
    "is_weekday": 0.02,
    "has_phone": 0.05,
    "has_price": 0.03,
    "is_question": 0.02,
    "has_renovation_keyword": 0.03,
    "has_urgency_words": 0.02,
    "hotness_hot": 0.08,
    "hotness_warm": 0.04,
    "budget_estimate": 0.06,
    "budget_high": 0.04,
    "timeline_asap": 0.07,
    "timeline_1_3_months": 0.04,
    "readiness_ready": 0.06,
    "readiness_planning": 0.03,
    "has_ai_summary": 0.03,
    "has_context_history": 0.04,
}

_model_weights: dict = {}
_model_bias: float = 0.0
_training_count: int = 0


def _load_weights():
    global _model_weights, _model_bias, _training_count
    if WEIGHTS_FILE.exists():
        try:
            data = json.loads(WEIGHTS_FILE.read_text())
            _model_weights = data.get("weights", DEFAULT_WEIGHTS.copy())
            _model_bias = data.get("bias", 0.0)
            _training_count = data.get("training_count", 0)
            logger.info("Loaded ML weights (%d training samples)", _training_count)
        except Exception:
            _model_weights = DEFAULT_WEIGHTS.copy()
    else:
        _model_weights = DEFAULT_WEIGHTS.copy()


def _save_weights():
    try:
        WEIGHTS_FILE.write_text(json.dumps({
            "weights": _model_weights,
            "bias": _model_bias,
            "training_count": _training_count,
            "updated_at": utcnow().isoformat(),
        }, indent=2))
    except Exception as e:
        logger.error("Failed to save ML weights: %s", e)


def _extract_features(lead: Lead, chat_stats: dict = None) -> dict:
    features = {}

    features["lead_score"] = min((lead.lead_score or 0) / 100.0, 1.0)
    features["urgency_high"] = 1.0 if lead.urgency == "high" else 0.0
    features["urgency_medium"] = 1.0 if lead.urgency == "medium" else 0.0
    features["urgency_low"] = 1.0 if lead.urgency == "low" else 0.0
    features["has_username"] = 1.0 if lead.username else 0.0
    features["has_reply"] = 1.0 if lead.reply_to_id else 0.0
    features["has_profile_link"] = 1.0 if lead.profile_link else 0.0

    if chat_stats and lead.chat_title in chat_stats:
        max_leads = max(chat_stats.values()) if chat_stats else 1
        features["chat_popularity"] = min(chat_stats[lead.chat_title] / max_leads, 1.0) if max_leads > 0 else 0.5
        features["chat_lead_count"] = min(chat_stats.get(lead.chat_title, 0) / 50.0, 1.0)
    else:
        features["chat_popularity"] = 0.5
        features["chat_lead_count"] = 0.0

    if lead.created_at:
        hour = lead.created_at.hour
        features["time_of_day"] = 1.0 if 9 <= hour <= 18 else 0.3
    else:
        features["time_of_day"] = 0.5

    text = lead.message_text or ""
    msg_len = len(text)
    features["message_length"] = min(msg_len / 500.0, 1.0)
    features["msg_exclamations"] = min(text.count("!") / 5.0, 1.0)
    features["msg_questions"] = min(text.count("?") / 3.0, 1.0)
    features["msg_word_count"] = min(len(text.split()) / 50.0, 1.0)

    if lead.reply_to_text and "?" in (lead.reply_to_text or ""):
        features["is_reply_to_question"] = 1.0
    else:
        features["is_reply_to_question"] = 0.0

    features["is_weekday"] = 1.0 if lead.created_at and lead.created_at.weekday() < 5 else 0.3

    text_lower = text.lower()
    features["has_phone"] = 1.0 if re.search(r'\+?\d{10,}', text) else 0.0
    features["has_price"] = 1.0 if re.search(r'(\d+[\s]*(?:руб|₽|тыс|млн|стоимость|цена|бюджет))', text, re.IGNORECASE) else 0.0
    features["is_question"] = 1.0 if text.strip().endswith('?') else 0.0

    renovation_kw = ['ремонт', 'отделк', 'дизайн', 'перепланировк', 'строитель', 'мастер', 'бригад']
    features["has_renovation_keyword"] = 1.0 if any(kw in text_lower for kw in renovation_kw) else 0.0

    features["has_urgency_words"] = 1.0 if re.search(r'(срочно|быстро|скидк|акци|сегодня|завтра)', text, re.IGNORECASE) else 0.0

    features["hotness_hot"] = 1.0 if lead.hotness == "hot" else 0.0
    features["hotness_warm"] = 1.0 if lead.hotness == "warm" else 0.0
    features["budget_estimate"] = 1.0 if lead.budget == "estimate" else 0.0
    features["budget_high"] = 1.0 if lead.budget == "high" else 0.0
    features["timeline_asap"] = 1.0 if lead.timeline == "asap" else 0.0
    features["timeline_1_3_months"] = 1.0 if lead.timeline == "1_3_months" else 0.0
    features["readiness_ready"] = 1.0 if lead.readiness == "ready" else 0.0
    features["readiness_planning"] = 1.0 if lead.readiness == "planning" else 0.0
    features["has_ai_summary"] = 1.0 if lead.ai_summary else 0.0
    features["has_context_history"] = 1.0 if (lead.reason and "контекст" in (lead.reason or "")) else 0.0

    return features


def _sigmoid(x: float) -> float:
    x = max(-500, min(500, x))
    return 1.0 / (1.0 + math.exp(-x))


def predict_probability(features: dict) -> float:
    z = _model_bias
    for fname, fval in features.items():
        w = _model_weights.get(fname, 0.0)
        z += w * fval
    prob = _sigmoid(z) * 100
    return min(max(round(prob, 1), 1), 99)


def get_feature_importance(features: dict) -> list:
    """Return feature contributions sorted by importance."""
    contributions = []
    for fname, fval in features.items():
        w = _model_weights.get(fname, 0.0)
        contribution = w * fval
        contributions.append((fname, contribution, fval))
    contributions.sort(key=lambda x: abs(x[1]), reverse=True)
    return contributions


async def score_lead(lead: Lead, tenant_id: int = None) -> dict:
    try:
        async with async_session() as session:
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
        ml_probability = predict_probability(features)
        
        ai_score = lead.lead_score or 50
        
        blended = ai_score * 0.75 + ml_probability * 0.25
        probability = min(max(round(blended, 1), 1), 99)

        return {
            "probability": probability,
            "ai_score": ai_score,
            "ml_raw": ml_probability,
            "features": features,
            "feature_importance": get_feature_importance(features),
            "recommendation": _get_recommendation(probability),
        }
    except Exception as e:
        logger.error("ML scoring error: %s", e)
        return {"probability": lead.lead_score or 50, "ai_score": lead.lead_score or 50, "ml_raw": 0, "features": {}, "recommendation": "Стандартная обработка"}


def _get_chat_stats_sync() -> dict:
    """Best-effort chat lead counts for feature extraction (avoids training/serving skew)."""
    try:
        import sqlite3
        from config import settings as _settings
        url = _settings.get_database_url()
        if "sqlite" not in url:
            return {}
        db_path = url.split("///", 1)[-1]
        from pathlib import Path
        p = Path(db_path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / db_path
        if not p.exists():
            return {}
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            "SELECT chat_title, COUNT(*) FROM leads WHERE status != 'deleted' GROUP BY chat_title"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def train_on_outcome(lead: Lead, is_positive: bool, learning_rate: float = 0.05):
    """Update weights based on actual outcome (deal closed or not)."""
    global _model_weights, _model_bias, _training_count

    features = _extract_features(lead, _get_chat_stats_sync())
    y_true = 1.0 if is_positive else 0.0
    y_pred = _sigmoid(
        _model_bias + sum(_model_weights.get(f, 0) * v for f, v in features.items())
    )
    error = y_true - y_pred

    _model_bias += learning_rate * error
    for fname, fval in features.items():
        if fname in _model_weights:
            _model_weights[fname] += learning_rate * error * fval

    _training_count += 1

    # Save on every update — in-memory weights would be lost on service restart
    _save_weights()
    if _training_count % 10 == 0:
        logger.info("ML weights saved (trained on %d samples)", _training_count)


async def retrain_model():
    """Retrain model on all historical leads with outcomes and feedback."""
    global _model_weights, _model_bias, _training_count

    logger.info("Starting ML model retraining...")

    async with async_session() as session:
        result = await session.execute(
            select(Lead).where(
                Lead.status.notin_(["deleted", "archive"]),
                Lead.created_at >= utcnow() - timedelta(days=180),
            )
        )
        leads = result.scalars().all()

    if len(leads) < 10:
        logger.info("Not enough training data (%d leads), skipping retrain", len(leads))
        return

    _model_weights = DEFAULT_WEIGHTS.copy()
    _model_bias = 0.0

    positive_leads = [l for l in leads if l.status == "deal" or l.feedback == "useful"]
    negative_leads = [l for l in leads if l.status in ("not_interested", "deleted") or l.feedback == "not_useful"]

    if not positive_leads:
        logger.info("No positive outcomes found, skipping retrain")
        return

    logger.info("Training on %d positive, %d negative samples", len(positive_leads), len(negative_leads))

    for epoch in range(5):
        for lead in positive_leads:
            train_on_outcome(lead, is_positive=True, learning_rate=0.01)
        for lead in negative_leads:
            train_on_outcome(lead, is_positive=False, learning_rate=0.01)

    _save_weights()
    logger.info("ML model retrained: %d samples, weights saved", _training_count)


async def get_model_metrics() -> dict:
    """Calculate model accuracy from recent leads with outcomes."""
    async with async_session() as session:
        recent = await session.execute(
            select(Lead).where(
                Lead.status.notin_(["deleted", "archive"]),
                Lead.created_at >= utcnow() - timedelta(days=30),
            )
        )
        leads = recent.scalars().all()

    if not leads:
        return {"accuracy": 0, "total": 0, "correct": 0}

    correct = 0
    total = 0
    for lead in leads:
        if lead.status in ("deal", "not_interested"):
            features = _extract_features(lead)
            prob = predict_probability(features)
            predicted_positive = prob >= 60
            actual_positive = lead.status == "deal"
            if predicted_positive == actual_positive:
                correct += 1
            total += 1

    accuracy = round(correct / total * 100, 1) if total > 0 else 0
    return {"accuracy": accuracy, "total": total, "correct": correct}


def _get_recommendation(probability: float) -> str:
    if probability >= 80:
        return "Высокая вероятность — срочно обработать!"
    if probability >= 60:
        return "Хороший лид — рекомендуется первый контакт"
    if probability >= 40:
        return "Средняя вероятность — стоит попробовать"
    return "Низкая вероятность — можно отложить"


_load_weights()
