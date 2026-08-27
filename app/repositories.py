from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence, Generic, TypeVar
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import selectinload

from sqlalchemy import select, func as sa_func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Tenant, User, Lead, LeadHistory, BlacklistedUser, ProcessedMessage, TelegramSession, TenantUsage, ActionLog,
    MessageTemplate, Webhook, LeadSource, EmployeeTarget, Commission, Penalty, WorkSession, ResponseTimeLog,
    Appointment, FollowUp, ManagerAction, ManagerContract, LeadTheftEvidence,
)
from config import utcnow

ModelType = TypeVar("ModelType")


class BaseRepository(Generic[ModelType]):
    """Base repository with automatic tenant_id scoping."""

    model: type[ModelType]

    def __init__(self, session: AsyncSession, tenant_id: int | None = None):
        self.session = session
        self.tenant_id = tenant_id

    def _tenant_filter(self, model):
        if self.tenant_id is not None:
            return model.tenant_id == self.tenant_id
        return True


class TenantRepository(BaseRepository):
    async def get_by_id(self, tenant_id: int) -> Optional[Tenant]:
        result = await self.session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[Tenant]:
        result = await self.session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> list[Tenant]:
        result = await self.session.execute(
            select(Tenant).order_by(Tenant.id)
        )
        return list(result.scalars().all())

    async def get_active(self) -> list[Tenant]:
        result = await self.session.execute(
            select(Tenant).where(Tenant.is_active == True).order_by(Tenant.id)
        )
        return list(result.scalars().all())

    async def create(self, name: str, slug: str, city: str = None, config: dict = None) -> Tenant:
        tenant = Tenant(name=name, slug=slug, city=city, config=config or {})
        self.session.add(tenant)
        await self.session.flush()
        return tenant

    async def update_config(self, tenant_id: int, config: dict) -> Optional[Tenant]:
        tenant = await self.get_by_id(tenant_id)
        if tenant:
            tenant.config = config
            flag_modified(tenant, "config")
            await self.session.flush()
        return tenant

    async def toggle_active(self, tenant_id: int) -> Optional[Tenant]:
        tenant = await self.get_by_id(tenant_id)
        if tenant:
            tenant.is_active = not tenant.is_active
            await self.session.flush()
        return tenant


class UserRepository(BaseRepository):
    async def get_by_id(self, user_id: int) -> Optional[User]:
        filters = [User.id == user_id]
        if self.tenant_id is not None:
            filters.append(User.tenant_id == self.tenant_id)
        result = await self.session.execute(select(User).where(*filters))
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> Optional[User]:
        filters = [User.username == username]
        if self.tenant_id is not None:
            filters.append(User.tenant_id == self.tenant_id)
        result = await self.session.execute(select(User).where(*filters))
        return result.scalar_one_or_none()

    async def get_super_admin(self, username: str) -> Optional[User]:
        result = await self.session.execute(
            select(User).where(User.username == username, User.role == "super_admin")
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[User]:
        filters = []
        if self.tenant_id is not None:
            filters.append(User.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(User).where(*filters).order_by(User.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_managers(self) -> list[User]:
        filters = [User.role.in_(["manager", "admin"]), User.is_active == True]
        if self.tenant_id is not None:
            filters.append(User.tenant_id == self.tenant_id)
        result = await self.session.execute(select(User).where(*filters))
        return list(result.scalars().all())

    async def list_active(self) -> list[User]:
        filters = [User.is_active == True]
        if self.tenant_id is not None:
            filters.append(User.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(User).where(*filters).order_by(User.role, User.username)
        )
        return list(result.scalars().all())

    async def create(self, **kwargs) -> User:
        if self.tenant_id is not None:
            kwargs["tenant_id"] = self.tenant_id
        user = User(**kwargs)
        self.session.add(user)
        await self.session.flush()
        return user

    async def count_leads(self, user_id: int) -> int:
        filters = [Lead.assigned_to == user_id, Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        return result.scalar_one() or 0

    async def get_lead_counts_by_user(self) -> dict[int, int]:
        filters = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(Lead.assigned_to, sa_func.count(Lead.id))
            .where(*filters)
            .group_by(Lead.assigned_to)
        )
        return {row[0]: row[1] for row in result.fetchall()}


class LeadRepository(BaseRepository):
    async def get_by_id(self, lead_id: int) -> Optional[Lead]:
        filters = [Lead.id == lead_id]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(select(Lead).where(*filters))
        return result.scalar_one_or_none()

    async def count(self, *extra_filters) -> int:
        filters = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        filters.extend(extra_filters)
        result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        return result.scalar_one() or 0

    async def count_by_status(self) -> list[tuple[str, int]]:
        filters = []
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(Lead.status, sa_func.count(Lead.id))
            .where(*filters)
            .group_by(Lead.status)
        )
        return result.fetchall()

    async def count_today(self, today_start: datetime) -> int:
        filters = [Lead.status != "deleted", Lead.created_at >= today_start]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        return result.scalar_one() or 0

    async def count_today_quality(self, today_start: datetime, min_score: float = 80) -> int:
        filters = [Lead.status != "deleted", Lead.created_at >= today_start, Lead.lead_score >= min_score]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        return result.scalar_one() or 0

    async def count_this_week(self, week_start: datetime) -> int:
        filters = [Lead.status != "deleted", Lead.created_at >= week_start]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        return result.scalar_one() or 0

    async def list_leads(
        self,
        status: str = None,
        chat: str = None,
        search: str = None,
        sort: str = "created_at",
        assigned_to: int = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Lead], int]:
        filters = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        if status:
            filters.append(Lead.status == status)
        if chat:
            filters.append(Lead.chat_title == chat)
        if assigned_to:
            filters.append(Lead.assigned_to == assigned_to)
        if search:
            search_pattern = f"%{search}%"
            filters.append(
                or_(
                    Lead.message_text.ilike(search_pattern),
                    Lead.first_name.ilike(search_pattern),
                    Lead.last_name.ilike(search_pattern),
                    Lead.username.ilike(search_pattern),
                    Lead.chat_title.ilike(search_pattern),
                    Lead.reply_to_text.ilike(search_pattern),
                    Lead.user_id.ilike(search_pattern),
                )
            )

        count_result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        total = count_result.scalar_one() or 0

        query = select(Lead).where(*filters)
        if sort == "score":
            query = query.order_by(Lead.lead_score.desc())
        elif sort == "urgency":
            from sqlalchemy import case
            urgency_order = case(
                (Lead.urgency == "high", 1),
                (Lead.urgency == "medium", 2),
                (Lead.urgency == "low", 3),
                else_=4,
            )
            query = query.order_by(urgency_order)
        elif sort == "name":
            query = query.order_by(Lead.first_name.asc())
        else:
            query = query.order_by(Lead.created_at.desc())
        query = query.offset(offset).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all()), total

    async def list_chats(self, assigned_to: int = None) -> list[str]:
        filters = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        if assigned_to:
            filters.append(Lead.assigned_to == assigned_to)
        result = await self.session.execute(
            select(Lead.chat_title).distinct().where(*filters).order_by(Lead.chat_title)
        )
        return [r[0] for r in result.fetchall()]

    async def list_recent(self, limit: int = 20) -> list[Lead]:
        filters = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(Lead).where(*filters).order_by(Lead.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def list_all(self) -> list[Lead]:
        filters = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(Lead).where(*filters).order_by(Lead.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_prev_next(self, lead_id: int) -> tuple[Optional[int], Optional[int]]:
        filters_base = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters_base.append(Lead.tenant_id == self.tenant_id)

        prev_q = select(Lead.id).where(Lead.id < lead_id, *filters_base).order_by(Lead.id.desc()).limit(1)
        next_q = select(Lead.id).where(Lead.id > lead_id, *filters_base).order_by(Lead.id.asc()).limit(1)
        prev_id = (await self.session.execute(prev_q)).scalar()
        next_id = (await self.session.execute(next_q)).scalar()
        return prev_id, next_id

    async def list_archive(
        self,
        chat: str = None,
        search: str = None,
        sort: str = "created_at",
        assigned_to: int = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Lead], int]:
        filters = [Lead.status == "archive"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        if chat:
            filters.append(Lead.chat_title == chat)
        if assigned_to:
            filters.append(Lead.assigned_to == assigned_to)
        if search:
            search_pattern = f"%{search}%"
            filters.append(
                or_(
                    Lead.message_text.ilike(search_pattern),
                    Lead.first_name.ilike(search_pattern),
                    Lead.last_name.ilike(search_pattern),
                    Lead.username.ilike(search_pattern),
                    Lead.chat_title.ilike(search_pattern),
                )
            )

        count_result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        total = count_result.scalar_one() or 0

        query = select(Lead).where(*filters)
        if sort == "score":
            query = query.order_by(Lead.lead_score.desc())
        elif sort == "name":
            query = query.order_by(Lead.first_name.asc())
        else:
            query = query.order_by(Lead.created_at.desc())
        query = query.offset(offset).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all()), total

    async def get_analytics(self) -> dict:
        filters = [Lead.status != "deleted"]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)

        by_chat_q = (
            select(Lead.chat_title, sa_func.count(Lead.id), sa_func.avg(Lead.lead_score))
            .where(*filters)
            .group_by(Lead.chat_title)
            .order_by(sa_func.count(Lead.id).desc())
        )
        by_status_q = (
            select(Lead.status, sa_func.count(Lead.id))
            .where(*filters)
            .group_by(Lead.status)
        )
        total_leads_q = select(sa_func.count(Lead.id)).where(*filters)
        avg_score_q = select(sa_func.avg(Lead.lead_score)).where(*filters)
        high_q = select(sa_func.count(Lead.id)).where(*filters, Lead.lead_score >= 90)
        med_q = select(sa_func.count(Lead.id)).where(
            *filters, and_(Lead.lead_score >= 70, Lead.lead_score < 90)
        )

        by_chat = (await self.session.execute(by_chat_q)).fetchall()
        by_status = (await self.session.execute(by_status_q)).fetchall()
        total_leads = (await self.session.execute(total_leads_q)).scalar() or 0
        avg_score = (await self.session.execute(avg_score_q)).scalar() or 0
        high_score = (await self.session.execute(high_q)).scalar() or 0
        medium_score = (await self.session.execute(med_q)).scalar() or 0

        return {
            "by_chat": by_chat,
            "by_status": by_status,
            "total_leads": total_leads,
            "avg_score": round(avg_score, 1) if avg_score else 0,
            "high_score": high_score,
            "medium_score": medium_score,
        }

    async def get_revenue_stats(self, days: int = 30) -> dict:
        since = utcnow() - timedelta(days=days)
        filters = [Lead.status != "deleted", Lead.deal_amount.isnot(None), Lead.deal_amount > 0]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)

        total_q = select(
            sa_func.count(Lead.id),
            sa_func.sum(Lead.deal_amount),
            sa_func.avg(Lead.deal_amount),
        ).where(*filters)

        period_filters = filters + [Lead.created_at >= since]
        period_q = select(
            sa_func.count(Lead.id),
            sa_func.sum(Lead.deal_amount),
            sa_func.avg(Lead.deal_amount),
        ).where(*period_filters)

        by_chat_q = (
            select(Lead.chat_title, sa_func.count(Lead.id), sa_func.sum(Lead.deal_amount))
            .where(*filters)
            .group_by(Lead.chat_title)
            .order_by(sa_func.sum(Lead.deal_amount).desc())
        )

        by_manager_q = (
            select(Lead.assigned_to, sa_func.count(Lead.id), sa_func.sum(Lead.deal_amount))
            .where(*filters)
            .group_by(Lead.assigned_to)
            .order_by(sa_func.sum(Lead.deal_amount).desc())
        )

        total = (await self.session.execute(total_q)).fetchone()
        period = (await self.session.execute(period_q)).fetchone()
        by_chat = (await self.session.execute(by_chat_q)).fetchall()
        by_manager = (await self.session.execute(by_manager_q)).fetchall()

        all_leads_q = select(sa_func.count(Lead.id))
        if self.tenant_id is not None:
            all_leads_q = all_leads_q.where(Lead.tenant_id == self.tenant_id, Lead.status != "deleted")
        total_all = (await self.session.execute(all_leads_q)).scalar() or 0

        return {
            "total_deals": total[0] or 0,
            "total_revenue": round(total[1] or 0, 2),
            "avg_deal": round(total[2] or 0, 2),
            "period_deals": period[0] or 0,
            "period_revenue": round(period[1] or 0, 2),
            "period_avg": round(period[2] or 0, 2),
            "conversion_rate": round((total[0] or 0) / total_all * 100, 1) if total_all > 0 else 0,
            "by_chat": [{"chat": r[0], "deals": r[1], "revenue": round(r[2] or 0, 2)} for r in by_chat],
            "by_manager": [{"user_id": r[0], "deals": r[1], "revenue": round(r[2] or 0, 2)} for r in by_manager],
        }

    async def set_deal_amount(self, lead_id: int, amount: float, currency: str = "RUB") -> Optional[Lead]:
        lead = await self.get_by_id(lead_id)
        if lead:
            lead.deal_amount = amount
            lead.deal_currency = currency
            lead.deal_closed_at = utcnow()
            if lead.status != "deal":
                lead.status = "deal"
            await self.session.flush()
            return lead
        return None

    async def create(self, **kwargs) -> Lead:
        if self.tenant_id is not None:
            kwargs["tenant_id"] = self.tenant_id
        lead = Lead(**kwargs)
        self.session.add(lead)
        await self.session.flush()
        return lead

    async def update_status(self, lead_id: int, new_status: str) -> Optional[Lead]:
        lead = await self.get_by_id(lead_id)
        if lead:
            old = lead.status
            lead.status = new_status
            await self.session.flush()
            return lead
        return None

    async def is_duplicate(self, user_id: int, dedup_days: int) -> bool:
        cutoff = utcnow() - timedelta(days=dedup_days)
        filters = [Lead.user_id == user_id, Lead.created_at >= cutoff]
        if self.tenant_id is not None:
            filters.append(Lead.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(sa_func.count(Lead.id)).where(*filters)
        )
        return (result.scalar_one() or 0) > 0


class LeadHistoryRepository(BaseRepository):
    async def list_for_lead(self, lead_id: int) -> list[LeadHistory]:
        filters = [LeadHistory.lead_id == lead_id]
        if self.tenant_id is not None:
            filters.append(LeadHistory.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(LeadHistory)
            .options(selectinload(LeadHistory.user))
            .where(*filters).order_by(LeadHistory.created_at.desc())
        )
        return list(result.scalars().all())

    async def create(self, **kwargs) -> LeadHistory:
        if self.tenant_id is not None:
            kwargs["tenant_id"] = self.tenant_id
        entry = LeadHistory(**kwargs)
        self.session.add(entry)
        await self.session.flush()
        return entry


class BlacklistedUserRepository(BaseRepository):
    async def is_blacklisted(self, user_id: int) -> bool:
        filters = [BlacklistedUser.user_id == user_id]
        if self.tenant_id is not None:
            filters.append(BlacklistedUser.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(sa_func.count(BlacklistedUser.id)).where(*filters)
        )
        return (result.scalar_one() or 0) > 0

    async def add(self, user_id: int, reason: str = None) -> BlacklistedUser:
        entry = BlacklistedUser(
            tenant_id=self.tenant_id,
            user_id=user_id,
            reason=reason,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry


class ProcessedMessageRepository(BaseRepository):
    async def is_processed(self, chat_title: str, message_id: int) -> bool:
        filters = [
            ProcessedMessage.chat_title == chat_title,
            ProcessedMessage.message_id == message_id,
        ]
        if self.tenant_id is not None:
            filters.append(ProcessedMessage.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(sa_func.count(ProcessedMessage.id)).where(*filters)
        )
        return (result.scalar_one() or 0) > 0

    async def mark(self, chat_title: str, message_id: int) -> ProcessedMessage:
        entry = ProcessedMessage(
            tenant_id=self.tenant_id,
            chat_title=chat_title,
            message_id=message_id,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def cleanup_old(self, days: int = 30) -> int:
        cutoff = utcnow() - timedelta(days=days)
        filters = [ProcessedMessage.created_at < cutoff]
        if self.tenant_id is not None:
            filters.append(ProcessedMessage.tenant_id == self.tenant_id)
        result = await self.session.execute(
            select(ProcessedMessage).where(*filters)
        )
        old = result.scalars().all()
        for entry in old:
            await self.session.delete(entry)
        await self.session.flush()
        return len(old)


class TelegramSessionRepository(BaseRepository[TelegramSession]):
    model = TelegramSession

    async def get_by_tenant(self, tenant_id: int) -> Optional[TelegramSession]:
        result = await self.session.execute(
            select(TelegramSession).where(TelegramSession.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def get_active_by_tenant(self, tenant_id: int) -> Optional[TelegramSession]:
        result = await self.session.execute(
            select(TelegramSession).where(
                TelegramSession.tenant_id == tenant_id,
                TelegramSession.is_active == True
            )
        )
        return result.scalar_one_or_none()

    async def create(self, tenant_id: int, session_name: str,
                     phone_number: str = None, **kwargs) -> TelegramSession:
        entry = TelegramSession(
            tenant_id=tenant_id,
            session_name=session_name,
            phone_number=phone_number,
            **kwargs
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def update_auth_status(self, tenant_id: int, is_authorized: bool,
                                 phone_number: str = None):
        entry = await self.get_by_tenant(tenant_id)
        if entry:
            entry.is_authorized = is_authorized
            if phone_number:
                entry.phone_number = phone_number
            await self.session.flush()
        return entry

    async def set_active(self, tenant_id: int, is_active: bool):
        entry = await self.get_by_tenant(tenant_id)
        if entry:
            entry.is_active = is_active
            await self.session.flush()
        return entry


class TenantUsageRepository(BaseRepository[TenantUsage]):
    model = TenantUsage

    async def log_event(self, tenant_id: int, event_type: str,
                        tokens_used: int = 0, model_used: str = None,
                        cost_usd: float = 0.0) -> TenantUsage:
        entry = TenantUsage(
            tenant_id=tenant_id,
            event_type=event_type,
            tokens_used=tokens_used,
            model_used=model_used,
            cost_usd=cost_usd,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def count_events_today(self, tenant_id: int, event_type: str) -> int:
        today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.count(TenantUsage.id)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.event_type == event_type,
                TenantUsage.created_at >= today_start
            )
        )
        return result.scalar() or 0

    async def sum_tokens_today(self, tenant_id: int) -> int:
        today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.sum(TenantUsage.tokens_used)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.created_at >= today_start
            )
        )
        return result.scalar() or 0

    async def sum_cost_today(self, tenant_id: int) -> float:
        today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.sum(TenantUsage.cost_usd)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.created_at >= today_start
            )
        )
        return result.scalar() or 0.0

    async def count_events_month(self, tenant_id: int, event_type: str) -> int:
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.count(TenantUsage.id)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.event_type == event_type,
                TenantUsage.created_at >= month_start
            )
        )
        return result.scalar() or 0

    async def sum_tokens_month(self, tenant_id: int) -> int:
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.sum(TenantUsage.tokens_used)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.created_at >= month_start
            )
        )
        return result.scalar() or 0

    async def sum_cost_month(self, tenant_id: int) -> float:
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.sum(TenantUsage.cost_usd)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.created_at >= month_start
            )
        )
        return result.scalar() or 0.0


class ActionLogRepository(BaseRepository[ActionLog]):
    model = ActionLog

    async def log(self, user_id: int, action_type: str, lead_id: int | None = None, meta: dict | None = None):
        entry = ActionLog(
            tenant_id=self.tenant_id,
            user_id=user_id,
            lead_id=lead_id,
            action_type=action_type,
            meta=meta,
        )
        self.session.add(entry)
        await self.session.flush()

    async def get_timeline(self, since: datetime, user_id: int | None = None, limit: int = 200):
        filters = [ActionLog.created_at >= since]
        if self.tenant_id is not None:
            filters.append(ActionLog.tenant_id == self.tenant_id)
        if user_id is not None:
            filters.append(ActionLog.user_id == user_id)
        result = await self.session.execute(
            select(ActionLog)
            .where(*filters)
            .order_by(ActionLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_summary_by_user(self, since: datetime):
        filters = [ActionLog.created_at >= since]
        if self.tenant_id is not None:
            filters.append(ActionLog.tenant_id == self.tenant_id)

        from sqlalchemy import case as sa_case
        login_cnt = sa_func.count(sa_case((ActionLog.action_type == "login", 1)))
        click_cnt = sa_func.count(sa_case((ActionLog.action_type == "click_write", 1)))
        status_cnt = sa_func.count(sa_case((ActionLog.action_type == "status_change", 1)))
        view_cnt = sa_func.count(sa_case((ActionLog.action_type == "lead_view", 1)))
        profile_cnt = sa_func.count(sa_case((ActionLog.action_type == "profile_click", 1)))
        export_cnt = sa_func.count(sa_case((ActionLog.action_type == "csv_export", 1)))
        last_login = sa_func.max(sa_case((ActionLog.action_type == "login", ActionLog.created_at), else_=None))

        q = (
            select(
                ActionLog.user_id,
                login_cnt.label("login_count"),
                click_cnt.label("click_write_count"),
                status_cnt.label("status_change_count"),
                view_cnt.label("lead_view_count"),
                profile_cnt.label("profile_click_count"),
                export_cnt.label("csv_export_count"),
                last_login.label("last_login"),
            )
            .where(*filters)
            .group_by(ActionLog.user_id)
        )
        rows = (await self.session.execute(q)).fetchall()

        return {
            row.user_id: {
                "login_count": row.login_count,
                "click_write_count": row.click_write_count,
                "status_change_count": row.status_change_count,
                "lead_view_count": row.lead_view_count,
                "profile_click_count": row.profile_click_count,
                "csv_export_count": row.csv_export_count,
                "last_login": row.last_login,
            }
            for row in rows
        }

    async def get_median_time_to_first_action(self, since: datetime):
        """Median time from lead creation to first click_write per user."""
        filters = [
            ActionLog.created_at >= since,
            ActionLog.action_type == "click_write",
            ActionLog.lead_id.isnot(None),
            ActionLog.user_id.isnot(None),
        ]
        if self.tenant_id is not None:
            filters.append(ActionLog.tenant_id == self.tenant_id)

        writes = (await self.session.execute(
            select(ActionLog).where(*filters)
        )).scalars().all()

        if not writes:
            return {}

        from app.models import Lead
        lead_ids = list({w.lead_id for w in writes})
        lead_filters = [Lead.id.in_(lead_ids)]
        if self.tenant_id is not None:
            lead_filters.append(Lead.tenant_id == self.tenant_id)
        leads_result = await self.session.execute(
            select(Lead.id, Lead.created_at).where(*lead_filters)
        )
        lead_map = {row.id: row.created_at for row in leads_result.fetchall()}

        from collections import defaultdict
        user_times: dict[int, list[float]] = defaultdict(list)
        for w in writes:
            lead_created = lead_map.get(w.lead_id)
            if lead_created and w.user_id:
                delta = (w.created_at - lead_created).total_seconds() / 3600
                if delta >= 0:
                    user_times[w.user_id].append(delta)

        result = {}
        for uid, times in user_times.items():
            times.sort()
            n = len(times)
            if n % 2 == 0:
                median = (times[n // 2 - 1] + times[n // 2]) / 2
            else:
                median = times[n // 2]
            result[uid] = {"median_hours": round(median, 1), "count": n}

        return result

    async def get_write_then_no_status_change(self, since: datetime, hours: int = 4):
        """Find click_write events where no status_change followed within N hours for same lead."""
        filters = [
            ActionLog.created_at >= since,
            ActionLog.action_type == "click_write",
            ActionLog.lead_id.isnot(None),
        ]
        if self.tenant_id is not None:
            filters.append(ActionLog.tenant_id == self.tenant_id)

        writes = (await self.session.execute(
            select(ActionLog).where(*filters).order_by(ActionLog.created_at.desc()).limit(200)
        )).scalars().all()

        if not writes:
            return []

        lead_ids = list({w.lead_id for w in writes})
        write_map: dict[int, list[ActionLog]] = {}
        for w in writes:
            write_map.setdefault(w.lead_id, []).append(w)

        sc_filters = [
            ActionLog.action_type == "status_change",
            ActionLog.lead_id.in_(lead_ids),
        ]
        if self.tenant_id is not None:
            sc_filters.append(ActionLog.tenant_id == self.tenant_id)
        status_changes = (await self.session.execute(
            select(ActionLog).where(*sc_filters)
        )).scalars().all()

        change_map: dict[int, list[datetime]] = {}
        for sc in status_changes:
            change_map.setdefault(sc.lead_id, []).append(sc.created_at)

        anomalies = []
        for w in writes:
            changes = change_map.get(w.lead_id, [])
            has_change = any(
                w.created_at < c <= w.created_at + timedelta(hours=hours)
                for c in changes
            )
            if not has_change:
                anomalies.append(w)

        return anomalies


class MessageTemplateRepository(BaseRepository):
    model = MessageTemplate

    async def list_active(self):
        q = select(MessageTemplate).where(
            MessageTemplate.is_active == True,
        )
        if self.tenant_id is not None:
            q = q.where(MessageTemplate.tenant_id == self.tenant_id)
        q = q.order_by(MessageTemplate.use_count.desc())
        return (await self.session.execute(q)).scalars().all()

    async def get_by_id(self, template_id: int):
        q = select(MessageTemplate).where(MessageTemplate.id == template_id)
        if self.tenant_id is not None:
            q = q.where(MessageTemplate.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalar_one_or_none()

    async def create(self, name: str, category: str, body: str, tenant_id: int = None):
        tmpl = MessageTemplate(
            tenant_id=tenant_id or self.tenant_id,
            name=name,
            category=category,
            body=body,
        )
        self.session.add(tmpl)
        await self.session.flush()
        return tmpl

    async def increment_use(self, template_id: int):
        tmpl = await self.get_by_id(template_id)
        if tmpl:
            tmpl.use_count = (tmpl.use_count or 0) + 1
        return tmpl


class WebhookRepository(BaseRepository):
    model = Webhook

    async def list_active(self):
        q = select(Webhook).where(Webhook.is_active == True)
        if self.tenant_id is not None:
            q = q.where(Webhook.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalars().all()

    async def get_by_id(self, webhook_id: int):
        q = select(Webhook).where(Webhook.id == webhook_id)
        if self.tenant_id is not None:
            q = q.where(Webhook.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalar_one_or_none()

    async def create(self, url: str, events: list, secret: str = None, tenant_id: int = None):
        wh = Webhook(
            tenant_id=tenant_id or self.tenant_id,
            url=url,
            events=events,
            secret=secret,
        )
        self.session.add(wh)
        await self.session.flush()
        return wh

    async def mark_triggered(self, webhook_id: int, success: bool):
        wh = await self.get_by_id(webhook_id)
        if wh:
            wh.last_triggered = utcnow()
            if not success:
                wh.fail_count = (wh.fail_count or 0) + 1
            else:
                wh.fail_count = 0
        return wh


class EmployeeTargetRepository(BaseRepository):
    model = EmployeeTarget

    async def get_or_create(self, user_id: int, period: str):
        q = select(EmployeeTarget).where(
            EmployeeTarget.user_id == user_id,
            EmployeeTarget.period == period,
        )
        if self.tenant_id is not None:
            q = q.where(EmployeeTarget.tenant_id == self.tenant_id)
        target = (await self.session.execute(q)).scalar_one_or_none()
        if not target:
            target = EmployeeTarget(
                tenant_id=self.tenant_id,
                user_id=user_id,
                period=period,
            )
            self.session.add(target)
            await self.session.flush()
        return target

    async def update_actuals(self, user_id: int, period: str, leads: int, deals: int, revenue: float):
        target = await self.get_or_create(user_id, period)
        target.actual_leads = leads
        target.actual_deals = deals
        target.actual_revenue = revenue
        return target

    async def list_all(self, period: str = None):
        q = select(EmployeeTarget)
        if self.tenant_id is not None:
            q = q.where(EmployeeTarget.tenant_id == self.tenant_id)
        if period:
            q = q.where(EmployeeTarget.period == period)
        return (await self.session.execute(q)).scalars().all()


class CommissionRepository(BaseRepository):
    model = Commission

    async def create(self, user_id: int, lead_id: int, period: str, deal_amount: float, rate: float, bonus: float = 0):
        comm = Commission(
            tenant_id=self.tenant_id,
            user_id=user_id,
            lead_id=lead_id,
            period=period,
            deal_amount=deal_amount,
            commission_rate=rate,
            commission_amount=deal_amount * rate / 100,
            bonus_amount=bonus,
        )
        self.session.add(comm)
        await self.session.flush()
        return comm

    async def list_by_user(self, user_id: int, period: str = None):
        q = select(Commission).where(Commission.user_id == user_id)
        if self.tenant_id is not None:
            q = q.where(Commission.tenant_id == self.tenant_id)
        if period:
            q = q.where(Commission.period == period)
        return (await self.session.execute(q)).scalars().all()

    async def list_all(self, period: str = None):
        q = select(Commission)
        if self.tenant_id is not None:
            q = q.where(Commission.tenant_id == self.tenant_id)
        if period:
            q = q.where(Commission.period == period)
        return (await self.session.execute(q)).scalars().all()

    async def get_summary(self, period: str):
        from sqlalchemy import func as sqlfunc
        q = select(
            Commission.user_id,
            sqlfunc.sum(Commission.commission_amount).label("total_commission"),
            sqlfunc.sum(Commission.bonus_amount).label("total_bonus"),
            sqlfunc.sum(Commission.deal_amount).label("total_deals"),
            sqlfunc.count(Commission.id).label("deals_count"),
        ).where(Commission.period == period)
        if self.tenant_id is not None:
            q = q.where(Commission.tenant_id == self.tenant_id)
        q = q.group_by(Commission.user_id)
        return (await self.session.execute(q)).all()

    async def mark_paid(self, commission_id: int):
        comm = await self.session.get(Commission, commission_id)
        if comm:
            comm.is_paid = True
        return comm


class PenaltyRepository(BaseRepository):
    model = Penalty

    async def create(self, user_id: int, reason: str, amount: float, description: str = None, lead_id: int = None):
        p = Penalty(
            tenant_id=self.tenant_id,
            user_id=user_id,
            reason=reason,
            amount=amount,
            description=description,
            lead_id=lead_id,
        )
        self.session.add(p)
        await self.session.flush()
        return p

    async def list_by_user(self, user_id: int):
        q = select(Penalty).where(Penalty.user_id == user_id)
        if self.tenant_id is not None:
            q = q.where(Penalty.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalars().all()

    async def get_total(self, user_id: int, period: str = None):
        from sqlalchemy import func as sqlfunc
        q = select(sqlfunc.sum(Penalty.amount)).where(Penalty.user_id == user_id)
        if self.tenant_id is not None:
            q = q.where(Penalty.tenant_id == self.tenant_id)
        if period:
            start = datetime.strptime(f"{period}-01", "%Y-%m-%d")
            end = datetime(start.year + 1, 1, 1) if start.month == 12 else datetime(start.year, start.month + 1, 1)
            q = q.where(Penalty.created_at >= start, Penalty.created_at < end)
        return (await self.session.execute(q)).scalar() or 0

    async def list_for_period(self, period: str = None):
        q = select(Penalty).order_by(Penalty.created_at.desc()).limit(50)
        if self.tenant_id is not None:
            q = q.where(Penalty.tenant_id == self.tenant_id)
        if period:
            start = datetime.strptime(f"{period}-01", "%Y-%m-%d")
            end = datetime(start.year + 1, 1, 1) if start.month == 12 else datetime(start.year, start.month + 1, 1)
            q = q.where(Penalty.created_at >= start, Penalty.created_at < end)
        return list((await self.session.execute(q)).scalars().all())


class WorkSessionRepository(BaseRepository):
    model = WorkSession

    async def get_today(self, user_id: int):
        from datetime import date
        today = date.today().isoformat()
        q = select(WorkSession).where(
            WorkSession.user_id == user_id,
            WorkSession.date == today,
        )
        if self.tenant_id is not None:
            q = q.where(WorkSession.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalar_one_or_none()

    async def record_login(self, user_id: int):
        from datetime import date
        today = date.today().isoformat()
        ws = await self.get_today(user_id)
        if not ws:
            ws = WorkSession(
                tenant_id=self.tenant_id,
                user_id=user_id,
                date=today,
                login_at=utcnow(),
            )
            self.session.add(ws)
        else:
            ws.login_at = ws.login_at or utcnow()
        await self.session.flush()
        return ws

    async def record_logout(self, user_id: int):
        ws = await self.get_today(user_id)
        if ws and not ws.logout_at:
            ws.logout_at = utcnow()
            if ws.login_at:
                ws.total_seconds = int((ws.logout_at - ws.login_at).total_seconds())
        return ws

    async def increment_actions(self, user_id: int):
        ws = await self.get_today(user_id)
        if ws:
            ws.actions_count = (ws.actions_count or 0) + 1
        return ws

    async def list_by_user(self, user_id: int, start_date: str = None, end_date: str = None):
        q = select(WorkSession).where(WorkSession.user_id == user_id)
        if self.tenant_id is not None:
            q = q.where(WorkSession.tenant_id == self.tenant_id)
        if start_date:
            q = q.where(WorkSession.date >= start_date)
        if end_date:
            q = q.where(WorkSession.date <= end_date)
        return (await self.session.execute(q)).scalars().all()

    async def list_all(self, date: str = None):
        q = select(WorkSession)
        if self.tenant_id is not None:
            q = q.where(WorkSession.tenant_id == self.tenant_id)
        if date:
            q = q.where(WorkSession.date == date)
        return (await self.session.execute(q)).scalars().all()


class ResponseTimeRepository(BaseRepository):
    model = ResponseTimeLog

    async def record_assignment(self, user_id: int, lead_id: int):
        rtl = ResponseTimeLog(
            tenant_id=self.tenant_id,
            user_id=user_id,
            lead_id=lead_id,
            assigned_at=utcnow(),
        )
        self.session.add(rtl)
        await self.session.flush()
        return rtl

    async def record_response(self, lead_id: int):
        q = select(ResponseTimeLog).where(ResponseTimeLog.lead_id == lead_id)
        if self.tenant_id is not None:
            q = q.where(ResponseTimeLog.tenant_id == self.tenant_id)
        rtl = (await self.session.execute(q)).scalar_one_or_none()
        if rtl and not rtl.first_response_at:
            rtl.first_response_at = utcnow()
            rtl.response_seconds = int((rtl.first_response_at - rtl.assigned_at).total_seconds())
        return rtl

    async def get_avg_response_time(self, user_id: int, days: int = 30):
        from sqlalchemy import func as sqlfunc
        from datetime import timedelta
        since = utcnow() - timedelta(days=days)
        q = select(sqlfunc.avg(ResponseTimeLog.response_seconds)).where(
            ResponseTimeLog.user_id == user_id,
            ResponseTimeLog.response_seconds.isnot(None),
            ResponseTimeLog.created_at >= since,
        )
        if self.tenant_id is not None:
            q = q.where(ResponseTimeLog.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalar() or 0

    async def list_by_user(self, user_id: int, days: int = 30):
        from datetime import timedelta
        since = utcnow() - timedelta(days=days)
        q = select(ResponseTimeLog).where(
            ResponseTimeLog.user_id == user_id,
            ResponseTimeLog.created_at >= since,
        )
        if self.tenant_id is not None:
            q = q.where(ResponseTimeLog.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalars().all()


class LeadSourceRepository(BaseRepository):
    model = LeadSource

    async def list_active(self):
        q = select(LeadSource).where(LeadSource.is_active == True)
        if self.tenant_id is not None:
            q = q.where(LeadSource.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalars().all()

    async def get_by_id(self, source_id: int):
        q = select(LeadSource).where(LeadSource.id == source_id)
        if self.tenant_id is not None:
            q = q.where(LeadSource.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalar_one_or_none()

    async def list_all(self):
        q = select(LeadSource)
        if self.tenant_id is not None:
            q = q.where(LeadSource.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalars().all()


class AppointmentRepository(BaseRepository):
    model = Appointment

    async def create(self, lead_id: int, user_id: int, title: str, scheduled_at,
                     description: str = None, duration_minutes: int = 30) -> Appointment:
        apt = Appointment(
            tenant_id=self.tenant_id,
            lead_id=lead_id,
            user_id=user_id,
            title=title,
            description=description,
            scheduled_at=scheduled_at,
            duration_minutes=duration_minutes,
        )
        self.session.add(apt)
        await self.session.flush()
        return apt

    async def get_by_id(self, apt_id: int):
        q = select(Appointment).where(Appointment.id == apt_id)
        if self.tenant_id is not None:
            q = q.where(Appointment.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalar_one_or_none()

    async def list_upcoming(self, user_id: int = None, days: int = 7):
        from datetime import timedelta
        since = utcnow()
        until = since + timedelta(days=days)
        q = select(Appointment).where(
            Appointment.status == "scheduled",
            Appointment.scheduled_at >= since,
            Appointment.scheduled_at <= until,
        )
        if self.tenant_id is not None:
            q = q.where(Appointment.tenant_id == self.tenant_id)
        if user_id:
            q = q.where(Appointment.user_id == user_id)
        q = q.order_by(Appointment.scheduled_at.asc())
        return (await self.session.execute(q)).scalars().all()

    async def get_pending_reminders(self):
        now = utcnow()
        q = select(Appointment).where(
            Appointment.status == "scheduled",
            Appointment.reminder_sent == False,
            Appointment.scheduled_at <= now,
        )
        if self.tenant_id is not None:
            q = q.where(Appointment.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalars().all()

    async def complete(self, apt_id: int):
        apt = await self.get_by_id(apt_id)
        if apt:
            apt.status = "completed"
            apt.completed_at = utcnow()
        return apt

    async def cancel(self, apt_id: int):
        apt = await self.get_by_id(apt_id)
        if apt:
            apt.status = "cancelled"
        return apt


class FollowUpRepository(BaseRepository):
    model = FollowUp

    async def create(self, lead_id: int, user_id: int, scheduled_at,
                     note: str = None) -> FollowUp:
        fu = FollowUp(
            tenant_id=self.tenant_id,
            lead_id=lead_id,
            user_id=user_id,
            scheduled_at=scheduled_at,
            note=note,
        )
        self.session.add(fu)
        await self.session.flush()
        return fu

    async def get_pending(self, user_id: int = None):
        now = utcnow()
        q = select(FollowUp).where(
            FollowUp.status == "pending",
            FollowUp.scheduled_at <= now,
        )
        if self.tenant_id is not None:
            q = q.where(FollowUp.tenant_id == self.tenant_id)
        if user_id:
            q = q.where(FollowUp.user_id == user_id)
        return (await self.session.execute(q)).scalars().all()

    async def complete(self, fu_id: int):
        fu = await self.session.get(FollowUp, fu_id)
        if fu:
            fu.status = "completed"
            fu.completed_at = utcnow()
        return fu

    async def cancel(self, fu_id: int):
        fu = await self.session.get(FollowUp, fu_id)
        if fu:
            fu.status = "cancelled"
        return fu

    async def list_by_lead(self, lead_id: int):
        q = select(FollowUp).where(FollowUp.lead_id == lead_id)
        if self.tenant_id is not None:
            q = q.where(FollowUp.tenant_id == self.tenant_id)
        q = q.order_by(FollowUp.scheduled_at.desc())
        return (await self.session.execute(q)).scalars().all()


# ==================== MANAGER ACTION TRACKING ====================

class ManagerActionRepository(BaseRepository):
    model = ManagerAction

    async def log_action(self, lead_id: int, user_id: int, action_type: str,
                         contact_type: str = None, client_response: str = None,
                         evidence: str = None, meta: dict = None) -> ManagerAction:
        """Log a manager action on a lead."""
        action = ManagerAction(
            tenant_id=self.tenant_id,
            lead_id=lead_id,
            user_id=user_id,
            action_type=action_type,
            contact_type=contact_type,
            client_response=client_response,
            evidence=evidence,
            meta=meta,
        )
        self.session.add(action)
        await self.session.flush()
        return action

    async def get_lead_actions(self, lead_id: int) -> list[ManagerAction]:
        """Get all actions for a specific lead."""
        q = select(ManagerAction).where(ManagerAction.lead_id == lead_id)
        if self.tenant_id is not None:
            q = q.where(ManagerAction.tenant_id == self.tenant_id)
        q = q.order_by(ManagerAction.created_at.desc())
        return (await self.session.execute(q)).scalars().all()

    async def get_manager_actions(self, user_id: int, days: int = 30) -> list[ManagerAction]:
        """Get all actions by a manager in the last N days."""
        since = utcnow() - timedelta(days=days)
        q = select(ManagerAction).where(
            ManagerAction.user_id == user_id,
            ManagerAction.created_at >= since,
        )
        if self.tenant_id is not None:
            q = q.where(ManagerAction.tenant_id == self.tenant_id)
        q = q.order_by(ManagerAction.created_at.desc())
        return (await self.session.execute(q)).scalars().all()

    async def has_action(self, lead_id: int, user_id: int, action_type: str) -> bool:
        """Check if a specific action exists for a lead."""
        q = select(ManagerAction).where(
            ManagerAction.lead_id == lead_id,
            ManagerAction.user_id == user_id,
            ManagerAction.action_type == action_type,
        )
        if self.tenant_id is not None:
            q = q.where(ManagerAction.tenant_id == self.tenant_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none() is not None

    async def get_actions_count(self, user_id: int, lead_id: int) -> int:
        """Count actions by a manager on a lead."""
        q = select(sa_func.count()).where(
            ManagerAction.lead_id == lead_id,
            ManagerAction.user_id == user_id,
        )
        if self.tenant_id is not None:
            q = q.where(ManagerAction.tenant_id == self.tenant_id)
        return (await self.session.execute(q)).scalar() or 0


# ==================== MANAGER CONTRACTS ====================

class ManagerContractRepository(BaseRepository):
    model = ManagerContract

    async def create(self, user_id: int, contract_number: str,
                     commission_rate: float = 0.0, min_deal_amount: float = 0.0,
                     penalty_per_stolen_lead: float = 10000.0,
                     penalty_per_sla_breach: float = 1000.0,
                     penalty_per_fake_reject: float = 5000.0,
                     sla_hours: int = 24,
                     expires_at: datetime = None) -> ManagerContract:
        """Create a new manager contract."""
        contract = ManagerContract(
            tenant_id=self.tenant_id,
            user_id=user_id,
            contract_number=contract_number,
            commission_rate=commission_rate,
            min_deal_amount=min_deal_amount,
            penalty_per_stolen_lead=penalty_per_stolen_lead,
            penalty_per_sla_breach=penalty_per_sla_breach,
            penalty_per_fake_reject=penalty_per_fake_reject,
            sla_hours=sla_hours,
            expires_at=expires_at,
        )
        self.session.add(contract)
        await self.session.flush()
        return contract

    async def get_active_contract(self, user_id: int) -> ManagerContract | None:
        """Get the active contract for a manager."""
        q = select(ManagerContract).where(
            ManagerContract.user_id == user_id,
            ManagerContract.is_active == True,
        )
        if self.tenant_id is not None:
            q = q.where(ManagerContract.tenant_id == self.tenant_id)
        result = await self.session.execute(q)
        return result.scalar_one_or_none()

    async def sign_contract(self, contract_id: int) -> ManagerContract:
        """Mark a contract as signed."""
        contract = await self.session.get(ManagerContract, contract_id)
        if contract:
            contract.signed_at = utcnow()
        return contract

    async def list_contracts(self, include_expired: bool = False) -> list[ManagerContract]:
        """List all contracts for this tenant."""
        q = select(ManagerContract)
        if self.tenant_id is not None:
            q = q.where(ManagerContract.tenant_id == self.tenant_id)
        if not include_expired:
            q = q.where(ManagerContract.is_active == True)
        q = q.order_by(ManagerContract.created_at.desc())
        return (await self.session.execute(q)).scalars().all()


# ==================== THEFT DETECTION ====================

class TheftDetectionRepository(BaseRepository):
    model = LeadTheftEvidence

    async def create_evidence(self, lead_id: int, suspect_user_id: int,
                              evidence_type: str, confidence: float,
                              details: dict = None) -> LeadTheftEvidence:
        """Create a theft evidence record."""
        evidence = LeadTheftEvidence(
            tenant_id=self.tenant_id,
            lead_id=lead_id,
            suspect_user_id=suspect_user_id,
            evidence_type=evidence_type,
            confidence=confidence,
            details=details,
        )
        self.session.add(evidence)
        await self.session.flush()
        return evidence

    async def confirm_evidence(self, evidence_id: int, confirmed_by: int,
                               penalty_id: int = None) -> LeadTheftEvidence:
        """Admin confirms theft evidence."""
        evidence = await self.session.get(LeadTheftEvidence, evidence_id)
        if evidence:
            evidence.is_confirmed = True
            evidence.confirmed_by = confirmed_by
            evidence.confirmed_at = utcnow()
            if penalty_id:
                evidence.penalty_id = penalty_id
        return evidence

    async def get_suspicious_activity(self, min_confidence: float = 50.0,
                                       confirmed_only: bool = False) -> list[LeadTheftEvidence]:
        """Get all suspicious activity above confidence threshold."""
        q = select(LeadTheftEvidence)
        if self.tenant_id is not None:
            q = q.where(LeadTheftEvidence.tenant_id == self.tenant_id)
        if confirmed_only:
            q = q.where(LeadTheftEvidence.is_confirmed == True)
        else:
            q = q.where(LeadTheftEvidence.confidence >= min_confidence)
        q = q.order_by(LeadTheftEvidence.created_at.desc())
        return (await self.session.execute(q)).scalars().all()

    async def get_user_evidence(self, user_id: int) -> list[LeadTheftEvidence]:
        """Get all evidence against a specific user."""
        q = select(LeadTheftEvidence).where(LeadTheftEvidence.suspect_user_id == user_id)
        if self.tenant_id is not None:
            q = q.where(LeadTheftEvidence.tenant_id == self.tenant_id)
        q = q.order_by(LeadTheftEvidence.created_at.desc())
        return (await self.session.execute(q)).scalars().all()

    async def detect_fake_rejects(self, days: int = 30) -> list[dict]:
        """Detect managers who rejected leads but clients returned."""
        since = utcnow() - timedelta(days=days)
        
        # Find leads rejected in the period
        rejected_q = select(Lead).where(
            Lead.status == "not_interested",
            Lead.assigned_to.isnot(None),
            Lead.updated_at >= since,
        )
        if self.tenant_id is not None:
            rejected_q = rejected_q.where(Lead.tenant_id == self.tenant_id)
        
        rejected_leads = (await self.session.execute(rejected_q)).scalars().all()
        results = []
        
        for lead in rejected_leads:
            # Check if same user_id appeared in another lead later
            if lead.user_id:
                reappear_q = select(Lead).where(
                    Lead.user_id == lead.user_id,
                    Lead.id != lead.id,
                    Lead.created_at > lead.updated_at,
                )
                if self.tenant_id is not None:
                    reappear_q = reappear_q.where(Lead.tenant_id == self.tenant_id)
                
                reappeared = (await self.session.execute(reappear_q)).scalar_one_or_none()
                if reappeared:
                    days_diff = (reappeared.created_at - lead.updated_at).days
                    confidence = min(90, 50 + (30 - days_diff) * 2)  # higher confidence if returned quickly
                    results.append({
                        "lead_id": lead.id,
                        "suspect_user_id": lead.assigned_to,
                        "evidence_type": "fake_reject",
                        "confidence": confidence,
                        "details": {
                            "reject_time": lead.updated_at.isoformat(),
                            "reappear_time": reappeared.created_at.isoformat(),
                            "days_diff": days_diff,
                            "new_lead_id": reappeared.id,
                        }
                    })
        
        return results

    async def detect_silent_takes(self, hours: int = 24) -> list[dict]:
        """Detect managers who took leads but never contacted them."""
        since = utcnow() - timedelta(hours=hours)
        
        # Find leads that were taken but have no actions
        q = select(Lead).where(
            Lead.status == "contacted",
            Lead.assigned_to.isnot(None),
            Lead.updated_at <= since,
        )
        if self.tenant_id is not None:
            q = q.where(Lead.tenant_id == self.tenant_id)
        
        stale_leads = (await self.session.execute(q)).scalars().all()
        results = []
        
        for lead in stale_leads:
            # Check if any manager action exists
            action_q = select(sa_func.count()).where(
                ManagerAction.lead_id == lead.id,
                ManagerAction.user_id == lead.assigned_to,
            )
            if self.tenant_id is not None:
                action_q = action_q.where(ManagerAction.tenant_id == self.tenant_id)
            
            action_count = (await self.session.execute(action_q)).scalar() or 0
            if action_count == 0:
                hours_since = (utcnow() - lead.updated_at).total_seconds() / 3600
                confidence = min(85, 40 + hours_since * 2)
                results.append({
                    "lead_id": lead.id,
                    "suspect_user_id": lead.assigned_to,
                    "evidence_type": "silent_take",
                    "confidence": confidence,
                    "details": {
                        "assigned_at": lead.updated_at.isoformat(),
                        "hours_since": round(hours_since, 1),
                    }
                })
        
        return results

    async def detect_quick_deals(self, min_hours: float = 1.0) -> list[dict]:
        """Detect suspiciously fast deal closures."""
        q = select(Lead).where(
            Lead.status == "deal",
            Lead.assigned_to.isnot(None),
            Lead.deal_closed_at.isnot(None),
        )
        if self.tenant_id is not None:
            q = q.where(Lead.tenant_id == self.tenant_id)
        
        deals = (await self.session.execute(q)).scalars().all()
        results = []
        
        for lead in deals:
            # Check time between assignment and deal closure
            assignment_q = select(LeadHistory).where(
                LeadHistory.lead_id == lead.id,
                LeadHistory.action.in_(["created", "auto_assigned", "reassigned"]),
            ).order_by(LeadHistory.created_at.asc()).limit(1)
            
            assignment = (await self.session.execute(assignment_q)).scalar_one_or_none()
            if assignment and lead.deal_closed_at:
                hours_diff = (lead.deal_closed_at - assignment.created_at).total_seconds() / 3600
                if hours_diff < min_hours:
                    # Check for intermediate actions
                    action_count = await ManagerActionRepository(self.session, self.tenant_id).get_actions_count(
                        lead.assigned_to, lead.id
                    )
                    if action_count <= 1:  # only "take lead" or nothing
                        confidence = min(80, 60 + (min_hours - hours_diff) * 10)
                        results.append({
                            "lead_id": lead.id,
                            "suspect_user_id": lead.assigned_to,
                            "evidence_type": "quick_deal",
                            "confidence": confidence,
                            "details": {
                                "assignment_time": assignment.created_at.isoformat(),
                                "deal_time": lead.deal_closed_at.isoformat(),
                                "hours_diff": round(hours_diff, 2),
                                "action_count": action_count,
                            }
                        })
        
        return results
