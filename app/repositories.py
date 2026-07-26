from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence, Generic, TypeVar
from sqlalchemy.orm.attributes import flag_modified

from sqlalchemy import select, func as sa_func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Tenant, User, Lead, LeadHistory, BlacklistedUser, ProcessedMessage, TelegramSession, TenantUsage, ActionLog,
    MessageTemplate, Webhook,
)

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
        cutoff = datetime.utcnow() - timedelta(days=dedup_days)
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
            select(LeadHistory).where(*filters).order_by(LeadHistory.created_at.desc())
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
        cutoff = datetime.utcnow() - timedelta(days=days)
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
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.count(TenantUsage.id)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.event_type == event_type,
                TenantUsage.created_at >= today_start
            )
        )
        return result.scalar() or 0

    async def sum_tokens_today(self, tenant_id: int) -> int:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.sum(TenantUsage.tokens_used)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.created_at >= today_start
            )
        )
        return result.scalar() or 0

    async def sum_cost_today(self, tenant_id: int) -> float:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.sum(TenantUsage.cost_usd)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.created_at >= today_start
            )
        )
        return result.scalar() or 0.0

    async def count_events_month(self, tenant_id: int, event_type: str) -> int:
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.count(TenantUsage.id)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.event_type == event_type,
                TenantUsage.created_at >= month_start
            )
        )
        return result.scalar() or 0

    async def sum_tokens_month(self, tenant_id: int) -> int:
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(sa_func.sum(TenantUsage.tokens_used)).where(
                TenantUsage.tenant_id == tenant_id,
                TenantUsage.created_at >= month_start
            )
        )
        return result.scalar() or 0

    async def sum_cost_month(self, tenant_id: int) -> float:
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
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
        last_login = sa_func.max(sa_case((ActionLog.action_type == "login", ActionLog.created_at), else_=None))

        q = (
            select(
                ActionLog.user_id,
                login_cnt.label("login_count"),
                click_cnt.label("click_write_count"),
                status_cnt.label("status_change_count"),
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
            wh.last_triggered = datetime.utcnow()
            if not success:
                wh.fail_count = (wh.fail_count or 0) + 1
            else:
                wh.fail_count = 0
        return wh
