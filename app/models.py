from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Float, Boolean, ForeignKey, func, Index
)
from datetime import datetime

from config import settings

_db_url = settings.get_database_url()
_is_postgres = "postgresql" in _db_url

_engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}
if _is_postgres:
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10
else:
    _engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(_db_url, **_engine_kwargs)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# Use JSONB for PostgreSQL, fallback to JSON for SQLite
if _is_postgres:
    from sqlalchemy.dialects.postgresql import JSONB as _JSONType
else:
    from sqlalchemy import JSON as _JSONType  # type: ignore


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    city = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    config = Column(_JSONType, default=dict, server_default="{}")
    created_at = Column(DateTime, server_default=func.now())

    users = relationship("User", back_populates="tenant")
    leads = relationship("Lead", back_populates="tenant")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_tenant_id", "tenant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(512), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(String(20), default="viewer")  # super_admin | admin | manager | viewer
    is_active = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)
    notified_about_logging = Column(Boolean, default=False)
    max_leads = Column(Integer, default=50)  # max active leads
    commission_rate = Column(Float, default=0.0)  # % from closed deals
    weight = Column(Float, default=1.0)  # for weighted auto-assignment
    telegram_id = Column(Integer, nullable=True)  # for bot notifications
    created_at = Column(DateTime, default=func.now())

    tenant = relationship("Tenant", back_populates="users")
    leads = relationship("Lead", back_populates="assignee")


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_tenant_id", "tenant_id"),
        Index("ix_leads_tenant_status", "tenant_id", "status"),
        Index("ix_leads_tenant_created", "tenant_id", "created_at"),
        Index("ix_leads_tenant_score", "tenant_id", "lead_score"),
        Index("ix_leads_status", "status"),
        Index("ix_leads_chat_title", "chat_title"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, nullable=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    profile_link = Column(String(512), nullable=True)
    message_text = Column(Text, nullable=False)
    reply_to_id = Column(Integer, nullable=True)
    reply_to_text = Column(Text, nullable=True)
    chat_title = Column(String(512), nullable=False)
    chat_username = Column(String(255), nullable=True)
    message_id = Column(Integer, nullable=True)
    lead_score = Column(Float, default=0)
    urgency = Column(String(20), default="low")
    reason = Column(Text, nullable=True)
    recommended_message = Column(Text, nullable=True)
    status = Column(String(30), default="new")
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    deal_amount = Column(Float, nullable=True)
    deal_currency = Column(String(10), default="RUB")
    deal_closed_at = Column(DateTime, nullable=True)
    is_notified = Column(Integer, default=0)
    feedback = Column(String(10), nullable=True)  # "useful" or "not_useful"
    feedback_reason = Column(String(50), nullable=True)  # spam, off_topic, duplicate, found_crew
    last_responded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    tenant = relationship("Tenant", back_populates="leads")
    assignee = relationship("User", back_populates="leads")
    history = relationship("LeadHistory", back_populates="lead", order_by="LeadHistory.created_at.desc()")

    def profile_url(self) -> str:
        if self.username:
            return f"https://t.me/{self.username}"
        if self.user_id:
            return f"tg://user?id={self.user_id}"
        return "#"

    def status_label(self) -> str:
        labels = {
            "new": "Новый",
            "contacted": "Контактировали",
            "missed_call": "Недозвон",
            "in_progress": "В работе",
            "interested": "Заинтересован",
            "not_interested": "Не интересует",
            "deal": "Сделка",
            "archive": "Архив",
            "deleted": "Удалён",
        }
        return labels.get(self.status, self.status)

    def score_color(self) -> str:
        if self.lead_score >= 90:
            return "#c45a5a"
        if self.lead_score >= 80:
            return "#c49a3c"
        if self.lead_score >= 70:
            return "#5a8f8f"
        return "#5a8f6a"


class LeadHistory(Base):
    __tablename__ = "lead_history"
    __table_args__ = (
        Index("ix_lead_history_tenant_id", "tenant_id"),
        Index("ix_lead_history_lead_id", "lead_id"),
        Index("ix_lead_history_tenant_lead", "tenant_id", "lead_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(50), nullable=False)
    old_value = Column(String(100), nullable=True)
    new_value = Column(String(100), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())

    lead = relationship("Lead", back_populates="history")


class BlacklistedUser(Base):
    __tablename__ = "blacklisted_users"
    __table_args__ = (
        Index("ix_blacklisted_users_tenant_id", "tenant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, nullable=False, index=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    __table_args__ = (
        Index("ix_processed_messages_tenant_id", "tenant_id"),
        Index("ix_processed_messages_chat_message", "chat_title", "message_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    chat_title = Column(String(512), nullable=False)
    message_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=func.now())


class TelegramSession(Base):
    __tablename__ = "telegram_sessions"
    __table_args__ = (
        Index("ix_telegram_sessions_tenant_id", "tenant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    session_name = Column(String(255), nullable=False)
    phone_number = Column(String(20), nullable=True)
    is_authorized = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    last_active = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class TenantUsage(Base):
    __tablename__ = "tenant_usage"
    __table_args__ = (
        Index("ix_tenant_usage_tenant_id", "tenant_id"),
        Index("ix_tenant_usage_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    event_type = Column(String(50), nullable=False)  # "ai_request", "lead_created", "message_scanned"
    tokens_used = Column(Integer, default=0)
    model_used = Column(String(100), nullable=True)
    cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, server_default=func.now())


class ActionLog(Base):
    __tablename__ = "action_logs"
    __table_args__ = (
        Index("ix_action_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_action_logs_tenant_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_action_logs_tenant_action", "tenant_id", "action_type"),
        Index("ix_action_logs_user_action", "user_id", "action_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    action_type = Column(String(50), nullable=False)
    meta = Column(_JSONType, nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        Index("ix_message_templates_tenant_id", "tenant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String(255), nullable=False)
    category = Column(String(50), default="general")  # first_contact, follow_up, deal_close, custom
    body = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    use_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())


class LeadSource(Base):
    __tablename__ = "lead_sources"
    __table_args__ = (
        Index("ix_lead_sources_tenant_id", "tenant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name = Column(String(100), nullable=False)  # "vk", "avito", "cian", "forumhouse"
    display_name = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)
    config = Column(_JSONType, default=dict)  # source-specific config (API keys, groups, etc.)
    last_synced = Column(DateTime, nullable=True)
    leads_found = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())


class Webhook(Base):
    __tablename__ = "webhooks"
    __table_args__ = (
        Index("ix_webhooks_tenant_id", "tenant_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    url = Column(String(1024), nullable=False)
    events = Column(_JSONType, default=list)  # ["lead_created", "status_change", "deal_closed"]
    secret = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    last_triggered = Column(DateTime, nullable=True)
    fail_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())


class EmployeeTarget(Base):
    __tablename__ = "employee_targets"
    __table_args__ = (
        Index("ix_employee_targets_user_id", "user_id"),
        Index("ix_employee_targets_period", "user_id", "period"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    period = Column(String(7), nullable=False)  # "2026-08"
    target_leads = Column(Integer, default=0)
    target_deals = Column(Integer, default=0)
    target_revenue = Column(Float, default=0.0)
    actual_leads = Column(Integer, default=0)
    actual_deals = Column(Integer, default=0)
    actual_revenue = Column(Float, default=0.0)
    created_at = Column(DateTime, default=func.now())


class Commission(Base):
    __tablename__ = "commissions"
    __table_args__ = (
        Index("ix_commissions_user_id", "user_id"),
        Index("ix_commissions_period", "user_id", "period"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)
    period = Column(String(7), nullable=False)  # "2026-08"
    deal_amount = Column(Float, default=0.0)
    commission_rate = Column(Float, default=0.0)
    commission_amount = Column(Float, default=0.0)
    bonus_amount = Column(Float, default=0.0)
    is_paid = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())


class Penalty(Base):
    __tablename__ = "penalties"
    __table_args__ = (
        Index("ix_penalties_user_id", "user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(String(50), nullable=False)  # missed_lead, slow_response, manual
    amount = Column(Float, default=0.0)
    description = Column(Text, nullable=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)
    created_at = Column(DateTime, default=func.now())


class WorkSession(Base):
    __tablename__ = "work_sessions"
    __table_args__ = (
        Index("ix_work_sessions_user_id", "user_id"),
        Index("ix_work_sessions_period", "user_id", "date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(String(10), nullable=False)  # "2026-08-06"
    login_at = Column(DateTime, nullable=True)
    logout_at = Column(DateTime, nullable=True)
    total_seconds = Column(Integer, default=0)
    actions_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())


class ResponseTimeLog(Base):
    __tablename__ = "response_time_logs"
    __table_args__ = (
        Index("ix_response_time_logs_user_id", "user_id"),
        Index("ix_response_time_logs_lead_id", "lead_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    assigned_at = Column(DateTime, nullable=False)
    first_response_at = Column(DateTime, nullable=True)
    response_seconds = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now())
