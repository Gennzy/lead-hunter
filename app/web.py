import json
import logging
import os
import re
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form, Query, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_, update, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Lead, User, LeadHistory, BlacklistedUser, Base, engine, async_session, Tenant, TelegramSession, MessageTemplate, Webhook
from app.repositories import (
    TenantRepository, UserRepository, LeadRepository,
    LeadHistoryRepository, BlacklistedUserRepository, TelegramSessionRepository,
    TenantUsageRepository, ActionLogRepository, MessageTemplateRepository, WebhookRepository,
)
from app.config_manager import TenantConfig
from app.auth import (
    get_current_user,
    hash_password,
    verify_password,
    create_token,
    COOKIE_NAME,
    seed_admin,
)
from app.security import (
    setup_security,
    generate_csrf_token,
    validate_csrf_token,
    sanitize_input,
    protect_env_file,
)
from config import settings

logger = logging.getLogger(__name__)

CHATS_FILE = Path("config_chats.json")


def _load_chats() -> list[str]:
    if CHATS_FILE.exists():
        return json.loads(CHATS_FILE.read_text())
    return []


def _save_chats(chats: list[str]):
    CHATS_FILE.write_text(json.dumps(chats, ensure_ascii=False, indent=2))


templates = Jinja2Templates(directory="app/templates")

MSK = timezone(timedelta(hours=3))

def _to_msk(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK)

templates.env.filters["msk"] = _to_msk


async def _get_theme_context(user) -> dict:
    if not user:
        return {"theme_color": None, "logo_url": None, "favicon_url": None, "tenant_name": None}
    
    tenant_id = user.tenant_id
    # For super_admin, auto-select first active tenant
    if not tenant_id and user.role == "super_admin":
        async with async_session() as session:
            result = await session.execute(select(Tenant).where(Tenant.is_active == True).limit(1))
            tenant = result.scalar_one_or_none()
            tenant_id = tenant.id if tenant else None
    
    if not tenant_id:
        return {"theme_color": None, "logo_url": None, "favicon_url": None, "tenant_name": None}
    
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = result.scalar_one_or_none()
    if not tenant:
        return {"theme_color": None, "logo_url": None, "favicon_url": None, "tenant_name": None}
    cfg = tenant.config or {}
    return {
        "theme_color": cfg.get("theme_color"),
        "logo_url": cfg.get("logo_url"),
        "favicon_url": cfg.get("favicon_url"),
        "tenant_name": cfg.get("company_name") or tenant.name,
    }


async def _template_ctx(user, **kwargs) -> dict:
    theme = await _get_theme_context(user)
    return {**theme, "user": user, **kwargs}


def _get_session_id(request: Request) -> str:
    sid = request.cookies.get("session_id")
    if sid:
        return sid
    return "default"


async def _get_tenant_id(user: User) -> int | None:
    """Get tenant_id from user. For super_admin, auto-select first active tenant."""
    if user.role == "super_admin":
        async with async_session() as session:
            result = await session.execute(
                select(Tenant).where(Tenant.is_active == True).limit(1)
            )
            tenant = result.scalar_one_or_none()
            return tenant.id if tenant else None
    return user.tenant_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    protect_env_file()

    # Seed default tenant + super_admin using the shared session
    async with async_session() as session:
        from app.models import Tenant, User
        from app.auth import hash_password, seed_admin as _sa
        import secrets, string

        result = await session.execute(select(Tenant).where(Tenant.slug == "default"))
        if not result.scalar_one_or_none():
            from config import KEYWORDS_LIST, NOISE_KEYWORDS_LIST
            t = Tenant(
                name="Default", slug="default", city="Санкт-Петербург",
                config={"keywords": KEYWORDS_LIST, "noise_keywords": NOISE_KEYWORDS_LIST,
                        "min_lead_score": 70, "system_prompt": ""},
            )
            session.add(t)
            await session.flush()
            print(f"[lifespan] Created default tenant (id={t.id})")

            # Generate admin password
            alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
            while True:
                pw = "".join(secrets.choice(alphabet) for _ in range(16))
                if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                        and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
                    break
            admin = User(username="admin", password_hash=hash_password(pw),
                         full_name="Super Admin", role="super_admin",
                         tenant_id=None, must_change_password=True)
            session.add(admin)
            print("=" * 60)
            print("[lifespan] SUPER ADMIN CREATED")
            print(f"  Username: admin")
            print(f"  Password: {pw}")
            print("=" * 60)
            await session.commit()
        else:
            print("[lifespan] Database already initialized.")

    await seed_admin()
    yield
    await engine.dispose()


app = FastAPI(title="Lead Hunter", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
setup_security(app)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = Query(None)):
    user = await get_current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)
    
    # Get theme color for white-label
    theme_color = "#7c5832"  # default
    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        default_tenant = await tenant_repo.get_by_slug("default")
        if default_tenant and default_tenant.config:
            theme_color = default_tenant.config.get("theme_color", theme_color)
    
    ctx = await _template_ctx(None, error=error, theme_color=theme_color)
    return templates.TemplateResponse(request, "login.html", ctx)


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/login?error=Неверный логин или пароль", status_code=303)

    async with async_session() as session:
        if user.tenant_id:
            await ActionLogRepository(session, user.tenant_id).log(
                user.id, "login",
                meta={"ip": request.client.host if request.client else None},
            )
            await session.commit()

    if user.must_change_password:
        token = create_token(user.id, user.tenant_id)
        response = RedirectResponse("/change-password", status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", max_age=3600)
        return response

    token = create_token(user.id, user.tenant_id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", max_age=86400 * settings.jwt_expire_hours // 24)
    return response


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    # Get theme color for white-label
    theme_color = "#7c5832"  # default
    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        default_tenant = await tenant_repo.get_by_slug("default")
        if default_tenant and default_tenant.config:
            theme_color = default_tenant.config.get("theme_color", theme_color)
    
    ctx = await _template_ctx(user, must_change=user.must_change_password, theme_color=theme_color)
    return templates.TemplateResponse(request, "change_password.html", ctx)


@app.post("/change-password")
async def change_password_submit(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if new_password != confirm_password:
        return RedirectResponse("/change-password?error=Пароли не совпадают", status_code=303)

    if len(new_password) < 8:
        return RedirectResponse("/change-password?error=Пароль должен быть не менее 8 символов", status_code=303)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        db_user = result.scalar_one()
        db_user.password_hash = hash_password(new_password)
        db_user.must_change_password = False
        await session.commit()

    token = create_token(user.id, user.tenant_id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", max_age=86400 * settings.jwt_expire_hours // 24)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/dismiss-logging-notice")
async def dismiss_logging_notice(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        db_user = result.scalar_one()
        db_user.notified_about_logging = True
        await session.commit()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    manager_filter = Lead.assigned_to == user.id if user.role == "manager" else None

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)

        total = await leads.count()
        new_today = await leads.count_today(today_start)
        new_week = await leads.count_this_week(week_start)

        status_rows = await leads.count_by_status()
        status_counts = {s: c for s, c in status_rows}

        # Top chats
        chat_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            chat_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            chat_filters.append(manager_filter)
        chat_q = (
            select(Lead.chat_title, func.count(Lead.id))
            .where(*chat_filters)
            .group_by(Lead.chat_title)
            .order_by(func.count(Lead.id).desc())
            .limit(10)
        )
        chat_leads = (await session.execute(chat_q)).fetchall()

        # Recent leads
        recent_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            recent_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            recent_filters.append(manager_filter)
        recent_q = select(Lead).where(*recent_filters).order_by(Lead.created_at.desc()).limit(20)
        recent_leads = (await session.execute(recent_q)).scalars().all()

        # Top-5 hottest leads
        hot_filters = [Lead.status != "deleted", Lead.status == "new"]
        if tenant_id is not None:
            hot_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            hot_filters.append(manager_filter)
        hot_q = select(Lead).where(*hot_filters).order_by(Lead.lead_score.desc()).limit(5)
        hot_leads = (await session.execute(hot_q)).scalars().all()

        # Overdue cutoff (> 4 hours without contact)
        overdue_cutoff = now - timedelta(hours=4)

        # Attention leads: overdue (operational priority)
        attention_filters = [Lead.status == "new"]
        if tenant_id is not None:
            attention_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            attention_filters.append(manager_filter)
        attention_q = (
            select(Lead)
            .where(and_(*attention_filters, Lead.created_at <= overdue_cutoff))
            .order_by(Lead.created_at.asc())
            .limit(5)
        )
        attention_leads = (await session.execute(attention_q)).scalars().all()

        # Unprocessed leads count
        unprocessed_filters = [Lead.status == "new"]
        if tenant_id is not None:
            unprocessed_filters.append(Lead.tenant_id == tenant_id)
        unprocessed_q = select(func.count(Lead.id)).where(*unprocessed_filters)
        unprocessed_count = (await session.execute(unprocessed_q)).scalar() or 0

        # Today stats
        processed_today_filters = [Lead.status.in_(["contacted", "in_progress", "deal", "interested"])]
        if tenant_id is not None:
            processed_today_filters.append(Lead.tenant_id == tenant_id)
        processed_today_q = select(func.count(Lead.id)).where(*processed_today_filters, Lead.updated_at >= today_start)
        processed_today = (await session.execute(processed_today_q)).scalar() or 0

        # Overdue leads count
        overdue_filters = [Lead.status == "new", Lead.created_at <= overdue_cutoff]
        if tenant_id is not None:
            overdue_filters.append(Lead.tenant_id == tenant_id)
        overdue_q = select(func.count(Lead.id)).where(*overdue_filters)
        overdue_count = (await session.execute(overdue_q)).scalar() or 0

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, total=total, new_today=new_today, new_week=new_week, status_counts=status_counts, chat_leads=chat_leads, recent_leads=recent_leads, hot_leads=hot_leads, attention_leads=attention_leads, unprocessed_count=unprocessed_count, processed_today=processed_today, overdue_count=overdue_count, csrf_token=csrf, now=datetime.utcnow())
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/leads", response_class=HTMLResponse)
async def leads_list(
    request: Request,
    lead_status: str = Query(None, alias="status"),
    chat: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    sort: str = Query(None),
    page: int = Query(1, ge=1),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    manager_id = user.id if user.role == "manager" else None
    page_size = 20

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)

        # Parse date filters
        df_date = None
        dt_date = None
        if date_from:
            try:
                df_date = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                pass
        if date_to:
            try:
                dt_date = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            except ValueError:
                pass

        # Build extra filters for the repository
        extra_filters = []
        if lead_status:
            extra_filters.append(Lead.status == lead_status)
        if chat:
            extra_filters.append(Lead.chat_title == chat)
        if search:
            safe_search = sanitize_input(search, 100)
            pattern = f"%{safe_search}%"
            from sqlalchemy import or_
            extra_filters.append(
                or_(
                    Lead.message_text.ilike(pattern),
                    Lead.first_name.ilike(pattern),
                    Lead.last_name.ilike(pattern),
                    Lead.username.ilike(pattern),
                    Lead.chat_title.ilike(pattern),
                    Lead.reply_to_text.ilike(pattern),
                )
            )
        if df_date:
            extra_filters.append(Lead.created_at >= df_date)
        if dt_date:
            extra_filters.append(Lead.created_at < dt_date)

        if lead_status == "deleted":
            count_filters = [Lead.status == "deleted"] + extra_filters
            if tenant_id is not None:
                count_filters.append(Lead.tenant_id == tenant_id)
            from sqlalchemy import func as sa_func
            total = (await session.execute(select(sa_func.count(Lead.id)).where(*count_filters))).scalar() or 0
        else:
            total = await leads.count(*extra_filters)

        # Build query
        q = select(Lead)
        if not lead_status:
            q = q.where(Lead.status != "deleted")
        if tenant_id is not None:
            q = q.where(Lead.tenant_id == tenant_id)
        if manager_id:
            q = q.where(Lead.assigned_to == manager_id)
        for f in extra_filters:
            q = q.where(f)

        if sort == "date":
            q = q.order_by(Lead.created_at.desc())
        elif sort == "urgency":
            q = q.order_by(case(
                (Lead.urgency == "high", 0),
                (Lead.urgency == "medium", 1),
                (Lead.urgency == "low", 2),
            ))
        elif sort == "name":
            q = q.order_by(Lead.first_name.asc())
        else:
            # Default: sort by score (hottest first)
            q = q.order_by(Lead.lead_score.desc())

        q = q.offset((page - 1) * page_size).limit(page_size)
        all_leads = (await session.execute(q)).scalars().all()

        total_pages = max(1, (total + page_size - 1) // page_size)

        # Status counts
        sc_filters = []
        if tenant_id is not None:
            sc_filters.append(Lead.tenant_id == tenant_id)
        if manager_id:
            sc_filters.append(Lead.assigned_to == manager_id)
        sc_rows = (await session.execute(
            select(Lead.status, func.count(Lead.id)).where(*sc_filters).group_by(Lead.status)
        )).fetchall()
        status_counts = {s: c for s, c in sc_rows}

        # Available chats
        ch_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            ch_filters.append(Lead.tenant_id == tenant_id)
        if manager_id:
            ch_filters.append(Lead.assigned_to == manager_id)
        available_chats = [r[0] for r in (await session.execute(
            select(Lead.chat_title).distinct().where(*ch_filters).order_by(Lead.chat_title)
        )).fetchall()]

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, leads=all_leads, total=total, page=page, total_pages=total_pages, status_filter=lead_status or "", chat_filter=chat or "", search=search or "", date_from=date_from or "", date_to=date_to or "", sort=sort or "", status_counts=status_counts, available_chats=available_chats, csrf_token=csrf, now=datetime.utcnow())
    return templates.TemplateResponse(request, "leads.html", ctx)


@app.get("/leads/export/csv")
async def leads_export_csv(
    request: Request,
    lead_status: str = Query(None, alias="status"),
    chat: str = Query(None),
    search: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
):
    import csv
    import io

    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)

    # Log CSV export (anti-theft)
    async with async_session() as session:
        await ActionLogRepository(session, tenant_id).log(
            user.id, "csv_export", meta={"filters": {"status": lead_status, "chat": chat}},
        )
        await session.commit()

    tenant_id = await _get_tenant_id(user)
    manager_id = user.id if user.role == "manager" else None

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)

        extra_filters = []
        if lead_status:
            extra_filters.append(Lead.status == lead_status)
        if chat:
            extra_filters.append(Lead.chat_title == chat)
        if search:
            safe_search = sanitize_input(search, 100)
            pattern = f"%{safe_search}%"
            from sqlalchemy import or_
            extra_filters.append(
                or_(
                    Lead.message_text.ilike(pattern),
                    Lead.first_name.ilike(pattern),
                    Lead.last_name.ilike(pattern),
                    Lead.username.ilike(pattern),
                    Lead.chat_title.ilike(pattern),
                )
            )
        if date_from:
            try:
                df_date = datetime.strptime(date_from, "%Y-%m-%d")
                extra_filters.append(Lead.created_at >= df_date)
            except ValueError:
                pass
        if date_to:
            try:
                dt_date = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
                extra_filters.append(Lead.created_at < dt_date)
            except ValueError:
                pass

        q = select(Lead)
        if not lead_status:
            q = q.where(Lead.status != "deleted")
        if tenant_id is not None:
            q = q.where(Lead.tenant_id == tenant_id)
        if manager_id:
            q = q.where(Lead.assigned_to == manager_id)
        for f in extra_filters:
            q = q.where(f)
        q = q.order_by(Lead.created_at.desc())
        all_leads = (await session.execute(q)).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Дата", "Имя", "Username", "Чат", "Score", "Срочность", "Статус", "Текст", "Причина"])
    for lead in all_leads:
        writer.writerow([
            lead.id,
            lead.created_at.strftime("%d.%m.%Y %H:%M") if lead.created_at else "",
            lead.first_name or "",
            lead.username or "",
            lead.chat_title or "",
            lead.lead_score or 0,
            lead.urgency or "",
            lead.status or "",
            (lead.message_text or "")[:200],
            lead.reason or "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=leads_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"},
    )


@app.get("/leads/new", response_class=HTMLResponse)
async def lead_create_form(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role == "viewer":
        return RedirectResponse("/leads", status_code=303)
    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, lead=None, csrf_token=csrf)
    return templates.TemplateResponse(request, "lead_form.html", ctx)


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(request: Request, lead_id: int, error: str = Query(None)):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)

        if not lead:
            return RedirectResponse("/leads", status_code=303)

        if user.role == "manager" and lead.assigned_to != user.id:
            return RedirectResponse("/leads", status_code=303)

        # Log lead view (anti-theft)
        await ActionLogRepository(session, tenant_id).log(
            user.id, "lead_view", lead_id=lead_id,
            meta={"chat": lead.chat_title, "score": lead.lead_score},
        )

        assignee = None
        if lead.assigned_to:
            users = UserRepository(session, tenant_id)
            assignee = await users.get_by_id(lead.assigned_to)

        history_repo = LeadHistoryRepository(session, tenant_id)
        history = await history_repo.list_for_lead(lead_id)

        prev_id, next_id = await leads.get_prev_next(lead_id)

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, lead=lead, assignee=assignee, history=history, prev_id=prev_id, next_id=next_id, csrf_token=csrf, error=error)
    return templates.TemplateResponse(request, "lead_detail.html", ctx)


@app.post("/leads/{lead_id}/feedback")
async def lead_feedback(request: Request, lead_id: int, feedback: str = Form(...), reason: str = Form("")):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)
        if not lead:
            return RedirectResponse("/leads", status_code=303)

        if feedback in ("useful", "not_useful"):
            lead.feedback = feedback
            lead.feedback_reason = reason if reason else None
            await session.commit()

    return RedirectResponse(f"/leads/{lead_id}?success=Оценка+сохранена", status_code=303)


@app.post("/leads/{lead_id}/log-write-click")
async def log_write_click(request: Request, lead_id: int):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)
        if not lead:
            raise HTTPException(status_code=404)

        if user.role == "manager" and lead.assigned_to != user.id:
            raise HTTPException(status_code=403)

        await ActionLogRepository(session, tenant_id).log(
            user.id, "click_write", lead_id=lead_id,
        )
        await session.commit()

    return {"ok": True}


@app.get("/archive", response_class=HTMLResponse)
async def archive_list(
    request: Request,
    search: str = Query(None),
    page: int = Query(1, ge=1),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    manager_id = user.id if user.role == "manager" else None
    page_size = 20

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)

        extra_filters = []
        if search:
            safe_search = sanitize_input(search, 100)
            pattern = f"%{safe_search}%"
            from sqlalchemy import or_
            extra_filters.append(
                or_(
                    Lead.message_text.ilike(pattern),
                    Lead.first_name.ilike(pattern),
                    Lead.last_name.ilike(pattern),
                    Lead.username.ilike(pattern),
                    Lead.chat_title.ilike(pattern),
                    Lead.reply_to_text.ilike(pattern),
                )
            )

        all_leads, total = await leads.list_archive(
            search=search,
            assigned_to=manager_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        total_pages = max(1, (total + page_size - 1) // page_size)

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, leads=all_leads, total=total, page=page, total_pages=total_pages, search=search or "", csrf_token=csrf)
    return templates.TemplateResponse(request, "archive.html", ctx)


@app.post("/leads/new")
async def lead_create(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    username: str = Form(""),
    phone: str = Form(""),
    message_text: str = Form(...),
    chat_title: str = Form(""),
    lead_score: int = Form(80),
    urgency: str = Form("medium"),
    reason: str = Form(""),
    recommended_message: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role == "viewer":
        return RedirectResponse("/leads", status_code=303)

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    first_name = sanitize_input(first_name, 100)
    last_name = sanitize_input(last_name, 100)
    username = sanitize_input(username, 100)
    phone = sanitize_input(phone, 20)
    message_text = sanitize_input(message_text, 5000)
    chat_title = sanitize_input(chat_title, 200)
    reason = sanitize_input(reason, 1000)
    recommended_message = sanitize_input(recommended_message, 2000)

    lead_score = max(0, min(100, lead_score))
    if urgency not in ("low", "medium", "high"):
        urgency = "medium"

    profile_link = None
    user_id = None
    if username:
        if all(c.isalnum() or c in "_" for c in username):
            profile_link = f"https://t.me/{username}"
    elif phone:
        profile_link = f"tel:{phone}"

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.create(
            user_id=user_id,
            username=username or None,
            first_name=first_name or None,
            last_name=last_name or None,
            profile_link=profile_link,
            message_text=message_text,
            chat_title=chat_title or "Ручное добавление",
            chat_username="",
            message_id=0,
            lead_score=lead_score,
            urgency=urgency,
            reason=reason or None,
            recommended_message=recommended_message or None,
            status="new",
            assigned_to=user.id,
        )

        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead.id,
            user_id=user.id,
            action="created",
            note=f"Создан пользователем {user.full_name or user.username}",
        )
        await session.commit()
    await _fire_webhooks(tenant_id, "lead_created", {"lead_id": lead.id, "score": lead.lead_score, "chat": lead.chat_title})
    return RedirectResponse("/leads?success=Лид+создан", status_code=303)


@app.post("/leads/{lead_id}/status")
async def update_status(
    request: Request,
    lead_id: int,
    lead_status: str = Form(..., alias="status"),
    deal_comment: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    valid_statuses = {"new", "contacted", "interested", "not_interested", "deal", "archive", "deleted"}
    if lead_status not in valid_statuses:
        return HTMLResponse(content="Invalid status", status_code=400)

    if lead_status == "deal" and not deal_comment.strip():
        return RedirectResponse(f"/leads/{lead_id}?error=Для статуса «Сделка» обязателен комментарий", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)

        if not lead:
            return RedirectResponse("/leads", status_code=303)

        if user.role == "manager" and lead.assigned_to != user.id:
            return RedirectResponse("/leads", status_code=303)

        old_status = lead.status
        lead.status = lead_status

        history = LeadHistoryRepository(session, tenant_id)
        history_note = f"{user.full_name or user.username} изменил статус"
        if deal_comment.strip():
            history_note += f": {deal_comment.strip()}"
        await history.create(
            lead_id=lead_id,
            user_id=user.id,
            action="status_change",
            old_value=old_status,
            new_value=lead_status,
            note=history_note,
        )
        await ActionLogRepository(session, tenant_id).log(
            user.id, "status_change", lead_id=lead_id,
            meta={"from": old_status, "to": lead_status},
        )
        await session.commit()
    await _fire_webhooks(tenant_id, "status_change", {"lead_id": lead_id, "from": old_status, "to": lead_status})
    if lead_status == "deal":
        await _fire_webhooks(tenant_id, "deal_closed", {"lead_id": lead_id, "score": lead.lead_score if lead else 0})
    return RedirectResponse(f"/leads/{lead_id}?success=Статус+изменён", status_code=303)


@app.post("/leads/{lead_id}/delete")
async def lead_delete(
    request: Request,
    lead_id: int,
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)
        if not lead:
            return RedirectResponse("/leads", status_code=303)
        if user.role == "manager" and lead.assigned_to != user.id:
            return RedirectResponse("/leads", status_code=303)

        if lead.user_id:
            bl = BlacklistedUserRepository(session, tenant_id)
            if not await bl.is_blacklisted(lead.user_id):
                await bl.add(
                    lead.user_id,
                    reason=f"Удалён лида #{lead_id} пользователем {user.full_name or user.username}",
                )

        old_status = lead.status
        lead.status = "deleted"

        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead_id,
            user_id=user.id,
            action="status_change",
            old_value=old_status,
            new_value="deleted",
            note=f"{user.full_name or user.username} удалил лид",
        )
        await session.commit()
    return RedirectResponse("/leads?success=Лид+удалён", status_code=303)


@app.post("/leads/bulk-action")
async def leads_bulk_action(
    request: Request,
    lead_ids: str = Form(""),
    action: str = Form(...),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    ids = [int(x) for x in lead_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return RedirectResponse("/leads", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        # Fetch leads scoped by tenant and optionally by manager
        q = select(Lead).where(Lead.id.in_(ids))
        if tenant_id is not None:
            q = q.where(Lead.tenant_id == tenant_id)
        if user.role == "manager":
            q = q.where(Lead.assigned_to == user.id)
        db_leads = (await session.execute(q)).scalars().all()
        valid_ids = [l.id for l in db_leads]

        history = LeadHistoryRepository(session, tenant_id)
        bl = BlacklistedUserRepository(session, tenant_id)
        action_log = ActionLogRepository(session, tenant_id)

        if action == "delete":
            for lid in valid_ids:
                lead_obj = await leads.get_by_id(lid)
                if lead_obj and lead_obj.user_id:
                    if not await bl.is_blacklisted(lead_obj.user_id):
                        await bl.add(
                            lead_obj.user_id,
                            reason=f"Массовое удаление пользователем {user.full_name or user.username}",
                        )
                old = lead_obj.status if lead_obj else None
                lead_obj.status = "deleted"
                await history.create(
                    lead_id=lid,
                    user_id=user.id,
                    action="status_change",
                    old_value=old,
                    new_value="deleted",
                    note=f"{user.full_name or user.username} (массовое удаление)",
                )
                await action_log.log(user.id, "status_change", lead_id=lid, meta={"from": old, "to": "deleted"})
        elif action in ("new", "contacted", "interested", "not_interested", "deal", "archive", "deleted"):
            for lid in valid_ids:
                lead_obj = await leads.get_by_id(lid)
                old = lead_obj.status if lead_obj else None
                lead_obj.status = action
                await history.create(
                    lead_id=lid,
                    user_id=user.id,
                    action="status_change",
                    old_value=old,
                    new_value=action,
                    note=f"{user.full_name or user.username} (массовое)",
                )
                await action_log.log(user.id, "status_change", lead_id=lid, meta={"from": old, "to": action})
        await session.commit()
    return RedirectResponse("/leads?success=Массовое+действие+выполнено", status_code=303)


@app.post("/leads/{lead_id}/note")
async def lead_add_note(
    request: Request,
    lead_id: int,
    note_text: str = Form(...),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    note_text = sanitize_input(note_text, 2000)
    if not note_text:
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)
        if not lead:
            return RedirectResponse("/leads", status_code=303)

        if user.role == "manager" and lead.assigned_to != user.id:
            return RedirectResponse("/leads", status_code=303)

        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead_id,
            user_id=user.id,
            action="note",
            note=note_text,
        )
        await session.commit()
    return RedirectResponse(f"/leads/{lead_id}?success=Заметка+добавлена", status_code=303)


@app.post("/leads/{lead_id}/assign")
async def lead_assign(
    request: Request,
    lead_id: int,
    assigned_to: int = Form(...),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)
        if not lead:
            return RedirectResponse("/leads", status_code=303)

        # Validate target user belongs to same tenant
        users = UserRepository(session, tenant_id)
        target_user = await users.get_by_id(assigned_to)
        if not target_user:
            return RedirectResponse(f"/leads/{lead_id}?error=Пользователь не найден", status_code=303)

        old_assignee = lead.assigned_to
        lead.assigned_to = assigned_to

        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead_id,
            user_id=user.id,
            action="reassigned",
            old_value=str(old_assignee),
            new_value=str(assigned_to),
            note=f"{user.full_name or user.username} назначил ответственного",
        )
        await session.commit()
    return RedirectResponse(f"/leads/{lead_id}?success=Лид+назначен", status_code=303)


@app.get("/users", response_class=HTMLResponse)
async def users_list(request: Request):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        users = UserRepository(session, tenant_id)
        all_users = await users.list_all()

        lead_counts = await users.get_lead_counts_by_user()

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, users=all_users, lead_counts=lead_counts, csrf_token=csrf)
    return templates.TemplateResponse(request, "users.html", ctx)


@app.post("/users/add")
async def user_add(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("manager"),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    username = sanitize_input(username, 100).strip()
    full_name = sanitize_input(full_name, 200)
    if role not in ("super_admin", "admin", "manager", "viewer"):
        role = "viewer"
    if user.role == "admin" and role == "super_admin":
        role = "admin"

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        users = UserRepository(session, tenant_id)
        existing = await users.get_by_username(username)
        if existing:
            return RedirectResponse("/users?error=Пользователь уже существует", status_code=303)

        await users.create(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name or None,
            role=role,
            tenant_id=user.tenant_id,
        )
        await session.commit()
    return RedirectResponse("/users?success=Пользователь+создан", status_code=303)


@app.post("/users/{user_id}/toggle")
async def user_toggle(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(admin)

    async with async_session() as session:
        users = UserRepository(session, tenant_id)
        target = await users.get_by_id(user_id)
        if target and target.id != admin.id:
            target.is_active = not target.is_active
            await session.commit()
    status_text = "активирован" if target and target.is_active else "деактивирован"
    return RedirectResponse(f"/users?success=Пользователь+{status_text}", status_code=303)


@app.post("/users/{user_id}/role")
async def user_change_role(
    request: Request,
    user_id: int,
    new_role: str = Form(...),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    if new_role not in ("super_admin", "admin", "manager", "viewer"):
        new_role = "viewer"

    if admin.role == "admin" and new_role == "super_admin":
        return RedirectResponse("/users?error=Нельзя назначить супер-админа", status_code=303)

    tenant_id = await _get_tenant_id(admin)

    async with async_session() as session:
        users = UserRepository(session, tenant_id)
        target = await users.get_by_id(user_id)
        if target:
            if target.id == admin.id:
                return RedirectResponse("/users?error=Нельзя изменить свою роль", status_code=303)
            if target.role == "super_admin" and admin.role != "super_admin":
                return RedirectResponse("/users?error=Нельзя изменить роль супер-админа", status_code=303)
            target.role = new_role
            await session.commit()
    return RedirectResponse("/users?success=Роль+изменена", status_code=303)


@app.post("/users/{user_id}/reset-password")
async def user_reset_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(admin)

    async with async_session() as session:
        users = UserRepository(session, tenant_id)
        target = await users.get_by_id(user_id)
        if not target:
            return RedirectResponse("/users?error=Пользователь не найден", status_code=303)
        target.password_hash = hash_password(new_password)
        target.must_change_password = True
        await session.commit()
    return RedirectResponse("/users?success=Пароль+сброшен", status_code=303)


@app.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        data = await leads.get_analytics()

        # Funnel data
        funnel_statuses = ["new", "contacted", "interested", "deal"]
        funnel_filters = []
        if tenant_id is not None:
            funnel_filters.append(Lead.tenant_id == tenant_id)
        funnel_q = select(Lead.status, func.count(Lead.id)).where(*funnel_filters).group_by(Lead.status)
        funnel_rows = (await session.execute(funnel_q)).fetchall()
        funnel_map = {s: c for s, c in funnel_rows}
        funnel = [(s, funnel_map.get(s, 0)) for s in funnel_statuses]

        # Source report: top chats by deal conversion
        source_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            source_filters.append(Lead.tenant_id == tenant_id)
        source_q = (
            select(Lead.chat_title, func.count(Lead.id), func.avg(Lead.lead_score))
            .where(*source_filters)
            .group_by(Lead.chat_title)
            .order_by(func.count(Lead.id).desc())
            .limit(15)
        )
        source_rows = (await session.execute(source_q)).fetchall()
        source_data = []
        for chat_title, count, avg_score in source_rows:
            deal_filters = [Lead.chat_title == chat_title, Lead.status == "deal"]
            if tenant_id is not None:
                deal_filters.append(Lead.tenant_id == tenant_id)
            deal_count = (await session.execute(select(func.count(Lead.id)).where(*deal_filters))).scalar() or 0
            conv = (deal_count / count * 100) if count > 0 else 0
            source_data.append({"chat": chat_title, "total": count, "deals": deal_count, "avg_score": round(avg_score or 0, 1), "conversion": round(conv, 1)})

    # Get feedback analysis
    from app.analyzer import analyze_feedback
    feedback_data = await analyze_feedback(tenant_id)

    ctx = await _template_ctx(user, by_chat=data["by_chat"], by_status=data["by_status"], avg_score=data["avg_score"], high_score=data["high_score"], medium_score=data["medium_score"], total=data["total_leads"], feedback_data=feedback_data, funnel=funnel, source_data=source_data)
    return templates.TemplateResponse(request, "analytics.html", ctx)


@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        users = UserRepository(session, tenant_id)
        active_users = await users.list_active()

        team_data = []
        for u in active_users:
            user_leads = LeadRepository(session, tenant_id)
            leads_total = await user_leads.count(Lead.assigned_to == u.id)

            # Status counts for this user
            sc_filters = [Lead.assigned_to == u.id, Lead.status != "deleted"]
            if tenant_id is not None:
                sc_filters.append(Lead.tenant_id == tenant_id)
            sc_rows = (await session.execute(
                select(Lead.status, func.count(Lead.id)).where(*sc_filters).group_by(Lead.status)
            )).fetchall()
            status_counts = {s: c for s, c in sc_rows}

            avg_score = (await session.execute(
                select(func.avg(Lead.lead_score)).where(*sc_filters)
            )).scalar() or 0

            high_leads = (await session.execute(
                select(func.count(Lead.id)).where(*sc_filters, Lead.lead_score >= 90)
            )).scalar() or 0

            last_lead = (await session.execute(
                select(Lead.created_at).where(Lead.assigned_to == u.id)
                .order_by(Lead.created_at.desc()).limit(1)
            )).scalar()

            deals = status_counts.get("deal", 0)
            contacted = status_counts.get("contacted", 0)
            interested = status_counts.get("interested", 0)
            new_leads = status_counts.get("new", 0)

            team_data.append({
                "user": u,
                "leads_total": leads_total,
                "status_counts": status_counts,
                "avg_score": round(avg_score, 1) if avg_score else 0,
                "high_leads": high_leads,
                "last_lead": last_lead,
                "deals": deals,
                "contacted": contacted,
                "interested": interested,
                "new_leads": new_leads,
            })

        total_team_leads = sum(d["leads_total"] for d in team_data)
        total_deals = sum(d["deals"] for d in team_data)

    ctx = await _template_ctx(user, team_data=team_data, total_team_leads=total_team_leads, total_deals=total_deals)
    return templates.TemplateResponse(request, "team.html", ctx)


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    user_id: int = Query(None),
):
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)
    since = datetime.utcnow() - timedelta(days=days)

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        all_users = await users_repo.list_active()
        user_map = {u.id: u for u in all_users}

        sla_hours = 4
        if tenant_id is not None:
            tenant_repo = TenantRepository(session)
            tenant = await tenant_repo.get_by_id(tenant_id)
            if tenant and tenant.config:
                sla_hours = tenant.config.get("sla_hours", 4)

        action_log = ActionLogRepository(session, tenant_id)
        summary = await action_log.get_summary_by_user(since)
        timeline = await action_log.get_timeline(since, user_id=user_id, limit=200)
        anomalies = await action_log.get_write_then_no_status_change(since, hours=sla_hours)
        median_data = await action_log.get_median_time_to_first_action(since)

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(
        current_user,
        all_users=all_users, user_map=user_map,
        summary=summary, timeline=timeline, anomalies=anomalies,
        median_data=median_data,
        days=days, selected_user_id=user_id, sla_hours=sla_hours,
        csrf_token=csrf,
    )
    return templates.TemplateResponse(request, "activity.html", ctx)


@app.get("/activity/pdf")
async def activity_pdf(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    user_id: int = Query(None),
):
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)
    since = datetime.utcnow() - timedelta(days=days)

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        all_users = await users_repo.list_active()
        user_map = {u.id: u for u in all_users}

        sla_hours = 4
        if tenant_id is not None:
            tenant_repo = TenantRepository(session)
            tenant = await tenant_repo.get_by_id(tenant_id)
            if tenant and tenant.config:
                sla_hours = tenant.config.get("sla_hours", 4)

        action_log = ActionLogRepository(session, tenant_id)
        summary = await action_log.get_summary_by_user(since)
        timeline = await action_log.get_timeline(since, user_id=user_id, limit=200)
        anomalies = await action_log.get_write_then_no_status_change(since, hours=sla_hours)
        median_data = await action_log.get_median_time_to_first_action(since)

    from fpdf import FPDF
    import io

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, f"Lead Hunter — Audit — {days}d", ln=True)

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, f"Generated: {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC", ln=True)
    if user_id and user_id in user_map:
        u = user_map[user_id]
        pdf.cell(0, 6, f"Employee: {(u.full_name or u.username or '')}", ln=True)
    else:
        pdf.cell(0, 6, "Employees: all", ln=True)
    pdf.ln(5)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Summary", ln=True)

    col_w = [45, 30, 18, 22, 25, 30]
    headers = ["Employee", "Last login", "Login", "Write", "Status", "Median"]
    pdf.set_font("Helvetica", "B", 9)
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, h, border=1, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    for u in all_users:
        s = summary.get(u.id, {})
        m = median_data.get(u.id, {})
        name = (u.full_name or u.username or "")[:20]
        last_login_val = s.get("last_login")
        last_login = last_login_val.strftime("%d.%m %H:%M") if last_login_val else "-"
        login_count = str(s.get("login_count", 0))
        write_count = str(s.get("click_write_count", 0))
        status_count = str(s.get("status_change_count", 0))
        median_str = f"{m['median_hours']}h ({m['count']})" if m else "-"

        vals = [name, last_login, login_count, write_count, status_count, median_str]
        for i, v in enumerate(vals):
            pdf.cell(col_w[i], 7, v, border=1, align="C" if i > 0 else "L")
        pdf.ln()

    pdf.ln(5)

    if anomalies:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, f"Anomalies ({len(anomalies)})", ln=True)
        pdf.set_font("Helvetica", "", 9)
        for a in anomalies[:50]:
            u = user_map.get(a.user_id)
            name = (u.full_name or u.username or f"ID:{a.user_id}") if u else f"ID:{a.user_id}"
            t = a.created_at.strftime("%d.%m %H:%M")
            pdf.cell(0, 6, f"{t}  {name}  Lead #{a.lead_id}  — {sla_hours}h+ no change", ln=True)
        pdf.ln(5)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, f"Timeline ({len(timeline)} entries)", ln=True)
    pdf.set_font("Helvetica", "", 8)

    for entry in timeline[:300]:
        t = entry.created_at.strftime("%d.%m %H:%M")
        u = user_map.get(entry.user_id)
        name = (u.full_name or u.username or f"ID:{entry.user_id}") if u else f"ID:{entry.user_id}"

        if entry.action_type == "login":
            action = "logged in"
        elif entry.action_type == "click_write":
            action = f"wrote Lead #{entry.lead_id}" if entry.lead_id else "wrote"
        elif entry.action_type == "status_change":
            fr = entry.meta.get("from", "?") if entry.meta else "?"
            to = entry.meta.get("to", "?") if entry.meta else "?"
            action = f"status {fr}->{to} Lead #{entry.lead_id}" if entry.lead_id else f"status {fr}->{to}"
        elif entry.action_type == "ai_request":
            action = "AI request"
        else:
            action = entry.action_type

        pdf.cell(0, 5, f"{t}  {name[:18]}  {action}", ln=True)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)

    filename = f"activity_{days}d_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/franchisor", response_class=HTMLResponse)
async def franchisor_page(request: Request):
    user = await get_current_user(request)
    if not user or user.role != "super_admin":
        return RedirectResponse("/login", status_code=303)

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        tenants = await tenant_repo.get_all()

        tenant_stats = []
        for t in tenants:
            leads_repo = LeadRepository(session, t.id)
            usage = TenantUsageRepository(session, t.id)
            total_leads = await leads_repo.count()
            leads_month = await usage.count_events_month(t.id, "lead_created")
            tokens_month = await usage.sum_tokens_month(t.id)
            cost_month = await usage.sum_cost_month(t.id)

            user_repo = UserRepository(session, t.id)
            all_users = await user_repo.list_all()
            user_count = len(all_users)

            tg_sessions = TelegramSessionRepository(session, t.id)
            tg = await tg_sessions.get_by_tenant(t.id)

            tenant_stats.append({
                "tenant": t,
                "user_count": user_count,
                "total_leads": total_leads,
                "leads_month": leads_month,
                "tokens_month": tokens_month,
                "cost_month": cost_month,
                "telegram_connected": tg is not None and tg.is_authorized,
            })

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, tenant_stats=tenant_stats, csrf_token=csrf)
    return templates.TemplateResponse(request, "franchisor.html", ctx)


@app.post("/franchisor/tenant/create")
async def franchisor_create_tenant(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    city: str = Form(""),
):
    user = await get_current_user(request)
    if not user or user.role != "super_admin":
        return RedirectResponse("/login", status_code=303)

    slug = slug.strip().lower()
    if len(slug) > 50:
        return RedirectResponse("/franchisor?error=Slug не может превышать 50 символов", status_code=303)
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", slug):
        return RedirectResponse("/franchisor?error=Slug: только строчные буквы, цифры и дефис (начинается с буквы/цифры)", status_code=303)
    _SLUG_RESERVED = {"admin", "api", "franchisor", "settings", "login", "static", "leads", "archive", "analytics", "team", "users", "billing", "telegram", "logout", "change-password", "new"}
    if slug in _SLUG_RESERVED:
        return RedirectResponse("/franchisor?error=Slug зарезервирован системой", status_code=303)

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        existing = await tenant_repo.get_by_slug(slug)
        if existing:
            return RedirectResponse("/franchisor?error=Slug уже занят", status_code=303)
        await tenant_repo.create(name=name, slug=slug, city=city or None)
        await session.commit()

    return RedirectResponse("/franchisor?success=Тенант создан", status_code=303)


@app.post("/franchisor/tenant/{tenant_id}/toggle")
async def franchisor_toggle_tenant(
    request: Request,
    tenant_id: int,
):
    user = await get_current_user(request)
    if not user or user.role != "super_admin":
        return RedirectResponse("/login", status_code=303)

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        tenant = await tenant_repo.toggle_active(tenant_id)
        if not tenant:
            return RedirectResponse("/franchisor?error=Тенант не найден", status_code=303)
        await session.commit()

    # If tenant was just deactivated — kill its monitor
    if not tenant.is_active:
        from app.main import cancel_monitor_for_tenant
        cancel_monitor_for_tenant(tenant_id)

    return RedirectResponse("/franchisor?success=Статус обновлён", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, error: str = Query(None), success: str = Query(None)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    chats = _load_chats()

    # Load tenant config
    tenant_config = {}
    async with async_session() as session:
        if tenant_id is not None:
            tenant_repo = TenantRepository(session)
            tenant = await tenant_repo.get_by_id(tenant_id)
            if tenant and tenant.config:
                tenant_config = tenant.config
        else:
            # Super admin — load default tenant config
            tenant_repo = TenantRepository(session)
            default_tenant = await tenant_repo.get_by_slug("default")
            if default_tenant and default_tenant.config:
                tenant_config = default_tenant.config

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, chats=chats, tenant_config=tenant_config, csrf_token=csrf, error=error, success=success)
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/settings/config")
async def update_tenant_config(
    request: Request,
    company_name: str = Form(""),
    city: str = Form(""),
    min_lead_score: int = Form(70),
    system_prompt: str = Form(""),
    owner_chat_id: str = Form(""),
    auto_assign: str = Form("false"),
    reminder_hours: int = Form(2),
    escalation_hours: int = Form(4),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(admin)

    if len(system_prompt) > 8000:
        return RedirectResponse("/settings?error=Системный промпт не может превышать 8000 символов", status_code=303)
    if len(company_name) > 200:
        return RedirectResponse("/settings?error=Название компании не может превышать 200 символов", status_code=303)

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        if tenant_id is not None:
            tenant = await tenant_repo.get_by_id(tenant_id)
        else:
            tenant = await tenant_repo.get_by_slug("default")

        if not tenant:
            return RedirectResponse("/settings?error=Тенант не найден", status_code=303)

        config = tenant.config or {}
        config["company_name"] = sanitize_input(company_name, 200)
        config["city"] = sanitize_input(city, 100)
        config["min_lead_score"] = max(0, min(100, min_lead_score))
        config["system_prompt"] = sanitize_input(system_prompt, 8000)
        config["auto_assign"] = auto_assign == "true"
        config["reminder_hours"] = max(1, min(48, reminder_hours))
        config["escalation_hours"] = max(1, min(72, escalation_hours))
        if owner_chat_id and owner_chat_id.strip():
            try:
                config["owner_chat_id"] = int(owner_chat_id.strip())
            except ValueError:
                return RedirectResponse("/settings?error=Telegram User ID должен быть числом", status_code=303)
        elif owner_chat_id is not None and not owner_chat_id.strip():
            config.pop("owner_chat_id", None)
        tenant.config = config
        flag_modified(tenant, "config")
        await session.commit()

    return RedirectResponse("/settings?success=Конфигурация сохранена", status_code=303)


@app.post("/settings/config/appearance")
async def update_tenant_appearance(
    request: Request,
    theme_color: str = Form(""),
    logo_url: str = Form(""),
    favicon_url: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(admin)

    if theme_color and not re.fullmatch(r"#[0-9a-fA-F]{6}", theme_color):
        return RedirectResponse("/settings?error=Цвет должен быть в формате #rrggbb", status_code=303)
    for url_val, url_name in [(logo_url, "Логотип"), (favicon_url, "Фавиконка")]:
        if url_val and not re.fullmatch(r"https?://[^\s<>\"']+", url_val):
            return RedirectResponse(f"/settings?error={url_name}: допустимы только HTTP/HTTPS URL", status_code=303)

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        if tenant_id is not None:
            tenant = await tenant_repo.get_by_id(tenant_id)
        else:
            tenant = await tenant_repo.get_by_slug("default")
        if not tenant:
            return RedirectResponse("/settings?error=Тенант не найден", status_code=303)

        config = tenant.config or {}
        if theme_color:
            config["theme_color"] = theme_color.lower()
        else:
            config.pop("theme_color", None)
        if logo_url:
            config["logo_url"] = sanitize_input(logo_url, 500)
        else:
            config.pop("logo_url", None)
        if favicon_url:
            config["favicon_url"] = sanitize_input(favicon_url, 500)
        else:
            config.pop("favicon_url", None)
        tenant.config = config
        flag_modified(tenant, "config")
        await session.commit()

    return RedirectResponse("/settings?success=Внешний вид сохранён", status_code=303)


@app.post("/settings/config/keywords")
async def update_tenant_keywords(
    request: Request,
    keywords: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(admin)

    # Parse keywords from textarea (one per line), normalize to lowercase
    kw_list = list({k.strip().lower() for k in keywords.splitlines() if k.strip()})[:500]

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        if tenant_id is not None:
            tenant = await tenant_repo.get_by_id(tenant_id)
        else:
            tenant = await tenant_repo.get_by_slug("default")

        if not tenant:
            return RedirectResponse("/settings?error=Тенант не найден", status_code=303)

        config = tenant.config or {}
        config["keywords"] = kw_list
        tenant.config = config
        flag_modified(tenant, "config")
        await session.commit()

    return RedirectResponse("/settings?success=Ключевые слова сохранены", status_code=303)


@app.post("/settings/config/noise-keywords")
async def update_tenant_noise_keywords(
    request: Request,
    noise_keywords: str = Form(""),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(admin)

    # Parse keywords from textarea (one per line), normalize to lowercase
    kw_list = list({k.strip().lower() for k in noise_keywords.splitlines() if k.strip()})[:500]

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        if tenant_id is not None:
            tenant = await tenant_repo.get_by_id(tenant_id)
        else:
            tenant = await tenant_repo.get_by_slug("default")

        if not tenant:
            return RedirectResponse("/settings?error=Тенант не найден", status_code=303)

        config = tenant.config or {}
        config["noise_keywords"] = kw_list
        tenant.config = config
        flag_modified(tenant, "config")
        await session.commit()

    return RedirectResponse("/settings?success=Шумовые слова сохранены", status_code=303)


@app.post("/settings/config/prices")
async def update_tenant_prices(
    request: Request,
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(admin)
    
    form = await request.form()
    price_list = {}
    
    categories = ['bathroom', 'kitchen', 'balcony', 'doors', 'windows', 'tiles', 'flooring', 'walls', 'ceiling', 'electric', 'plumbing']
    
    for cat in categories:
        min_val = form.get(f"price_{cat}_min", "")
        max_val = form.get(f"price_{cat}_max", "")
        unit = form.get(f"price_{cat}_unit", "₽/м²")
        
        if min_val or max_val:
            try:
                min_int = int(min_val) if min_val else 0
                max_int = int(max_val) if max_val else 0
            except (ValueError, TypeError):
                return RedirectResponse(f"/settings?error=Прайс: введите числа для {cat}", status_code=303)
            if min_int > 0 and max_int > 0 and min_int > max_int:
                return RedirectResponse(f"/settings?error=Прайс: минимум больше максимума для {cat}", status_code=303)
            price_list[cat] = {
                "min": min_int,
                "max": max_int,
                "unit": unit,
            }

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        if tenant_id is not None:
            tenant = await tenant_repo.get_by_id(tenant_id)
        else:
            tenant = await tenant_repo.get_by_slug("default")

        if not tenant:
            return RedirectResponse("/settings?error=Тенант не найден", status_code=303)

        config = tenant.config or {}
        config["price_list"] = price_list
        tenant.config = config
        flag_modified(tenant, "config")
        await session.commit()

    return RedirectResponse("/settings?success=Прайс-лист сохранён", status_code=303)


@app.post("/settings/chats/add")
async def add_chat(
    request: Request,
    chat: str = Form(...),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    chat = sanitize_input(chat, 200).strip()
    if not chat:
        return RedirectResponse("/settings", status_code=303)

    chats = _load_chats()

    normalized = chat.lower().replace("https://", "").replace("http://", "").replace("t.me/", "").rstrip("/")
    is_duplicate = False
    for existing in chats:
        existing_norm = existing.lower().replace("https://", "").replace("http://", "").replace("t.me/", "").rstrip("/")
        if normalized == existing_norm or normalized in existing_norm or existing_norm in normalized:
            is_duplicate = True
            break

    if is_duplicate:
        return RedirectResponse("/settings?error=Такой чат уже добавлен", status_code=303)

    if len(chats) >= 100:
        return RedirectResponse("/settings?error=Максимум 100 чатов", status_code=303)

    chats.append(chat)
    _save_chats(chats)
    return RedirectResponse("/settings?success=Чат+добавлен", status_code=303)


@app.post("/settings/chats/remove")
async def remove_chat(
    request: Request,
    chat: str = Form(...),
    csrf_token: str = Form(""),
):
    admin = await get_current_user(request)
    if not admin or admin.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    chat = sanitize_input(chat, 200).strip()
    chats = _load_chats()
    if chat in chats:
        chats.remove(chat)
        _save_chats(chats)
    return RedirectResponse("/settings?success=Чат+удалён", status_code=303)


@app.get("/api/stats")
async def api_stats(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)

        return {
            "total": await leads.count(),
            "today": await leads.count_today(today_start),
            "this_week": await leads.count_this_week(week_start),
        }


# ===== Telegram Session Management =====

@app.get("/settings/telegram")
async def telegram_sessions_page(request: Request, error: str = Query(None), success: str = Query(None), password_needed: str = Query(None)):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        sessions = TelegramSessionRepository(session, tenant_id)
        telegram_session = await sessions.get_by_tenant(tenant_id)

    ctx = await _template_ctx(user, csrf_token=generate_csrf_token(_get_session_id(request)), telegram_session=telegram_session, error=error, success=success, password_needed=bool(password_needed))
    return templates.TemplateResponse(request, "telegram_sessions.html", ctx)


@app.post("/settings/telegram/create")
async def create_telegram_session(
    request: Request,
    csrf_token: str = Form(...),
    phone_number: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    if not tenant_id:
        return RedirectResponse("/settings/telegram?error=Super admin must select a tenant", status_code=303)

    phone_number = sanitize_input(phone_number, 20).strip()
    if not phone_number:
        return RedirectResponse("/settings/telegram?error=Phone number is required", status_code=303)
    if ":" in phone_number or not phone_number.startswith("+"):
        return RedirectResponse("/settings/telegram?error=Enter a phone number like +79991234567, not a bot token", status_code=303)

    session_name = f"session_{tenant_id}_{phone_number.replace('+', '')}"

    async with async_session() as session:
        sessions = TelegramSessionRepository(session, tenant_id)
        existing = await sessions.get_by_tenant(tenant_id)
        if existing:
            return RedirectResponse("/settings/telegram?error=Session already exists for this tenant", status_code=303)

        await sessions.create(
            tenant_id=tenant_id,
            session_name=session_name,
            phone_number=phone_number,
        )
        await session.commit()

    # Create Telethon client and send code request
    from app.telegram_factory import client_factory
    from app.config_manager import TenantConfig
    tenant_config = await TenantConfig.create(tenant_id)
    client = client_factory.create_client(tenant_id, session_name, tenant_config)
    result = await client_factory.send_code(tenant_id, phone_number)

    if "error" in result:
        return RedirectResponse(f"/settings/telegram?error={result['error']}", status_code=303)

    return RedirectResponse("/settings/telegram?success=Code sent! Check your Telegram.", status_code=303)


@app.post("/settings/telegram/authorize")
async def authorize_telegram_session(
    request: Request,
    csrf_token: str = Form(...),
    code: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    if not tenant_id:
        return RedirectResponse("/settings/telegram?error=Super admin must select a tenant", status_code=303)

    async with async_session() as session:
        sessions = TelegramSessionRepository(session, tenant_id)
        telegram_session = await sessions.get_by_tenant(tenant_id)
        if not telegram_session:
            return RedirectResponse("/settings/telegram?error=No session found", status_code=303)

    # Use factory to sign in with code
    from app.telegram_factory import client_factory
    result = await client_factory.sign_in(tenant_id, telegram_session.phone_number, code)

    if result.get("status") == "password_needed":
        return RedirectResponse("/settings/telegram?password_needed=1", status_code=303)

    if "error" in result:
        return RedirectResponse(f"/settings/telegram?error={result['error']}", status_code=303)

    # Mark as authorized
    async with async_session() as session:
        sessions = TelegramSessionRepository(session, tenant_id)
        await sessions.update_auth_status(tenant_id, True)
        await session.commit()

    return RedirectResponse("/settings/telegram?success=Telegram authorized successfully!", status_code=303)


@app.post("/settings/telegram/authorize-password")
async def authorize_telegram_password(
    request: Request,
    csrf_token: str = Form(...),
    password: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    if not tenant_id:
        return RedirectResponse("/settings/telegram?error=Super admin must select a tenant", status_code=303)

    from app.telegram_factory import client_factory
    result = await client_factory.sign_in_password(tenant_id, password)

    if "error" in result:
        return RedirectResponse(f"/settings/telegram?error={result['error']}", status_code=303)

    async with async_session() as session:
        sessions = TelegramSessionRepository(session, tenant_id)
        await sessions.update_auth_status(tenant_id, True)
        await session.commit()

    return RedirectResponse("/settings/telegram?success=Telegram authorized successfully!", status_code=303)


@app.post("/settings/telegram/delete")
async def delete_telegram_session(
    request: Request,
    csrf_token: str = Form(...),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    if not tenant_id:
        return RedirectResponse("/settings/telegram?error=Super admin must select a tenant", status_code=303)

    # Stop client if running
    from app.telegram_factory import client_factory
    await client_factory.stop_client(tenant_id)

    # Delete from DB
    async with async_session() as session:
        sessions = TelegramSessionRepository(session, tenant_id)
        telegram_session = await sessions.get_by_tenant(tenant_id)
        if telegram_session:
            await session.delete(telegram_session)
            await session.commit()

    return RedirectResponse("/settings/telegram?success=Session deleted", status_code=303)


# ===== Billing =====

@app.get("/billing")
async def billing_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    tenant_id = await _get_tenant_id(user)
    if not tenant_id:
        return RedirectResponse("/?error=Super admin must select a tenant", status_code=303)

    from app.config_manager import TenantConfig
    tenant_config = await TenantConfig.create(tenant_id)

    async with async_session() as session:
        usage = TenantUsageRepository(session, tenant_id)

        ai_today = await usage.count_events_today(tenant_id, "ai_request")
        tokens_today = await usage.sum_tokens_today(tenant_id)
        cost_today = await usage.sum_cost_today(tenant_id)

        ai_month = await usage.count_events_month(tenant_id, "ai_request")
        tokens_month = await usage.sum_tokens_month(tenant_id)
        cost_month = await usage.sum_cost_month(tenant_id)

        leads_month = await usage.count_events_month(tenant_id, "lead_created")

    ctx = await _template_ctx(user, csrf_token=generate_csrf_token(_get_session_id(request)), ai_today=ai_today, tokens_today=tokens_today, cost_today=cost_today, ai_month=ai_month, tokens_month=tokens_month, cost_month=cost_month, leads_month=leads_month, max_ai_per_day=tenant_config.max_ai_requests_per_day, max_tokens_per_day=tenant_config.max_tokens_per_day, max_cost_per_month=tenant_config.max_cost_per_month_usd, max_leads_per_month=tenant_config.max_leads_per_month, ai_enabled=tenant_config.ai_enabled)
    return templates.TemplateResponse(request, "billing.html", ctx)


@app.post("/settings/billing")
async def update_billing_settings(
    request: Request,
    csrf_token: str = Form(...),
    max_ai_requests_per_day: int = Form(...),
    max_tokens_per_day: int = Form(...),
    max_cost_per_month_usd: float = Form(...),
    max_leads_per_month: int = Form(...),
    ai_enabled: bool = Form(True),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    if not tenant_id:
        return RedirectResponse("/billing?error=Super admin must select a tenant", status_code=303)

    # Validate limits
    max_ai_requests_per_day = max(1, min(10000, max_ai_requests_per_day))
    max_tokens_per_day = max(1000, min(10000000, max_tokens_per_day))
    max_cost_per_month_usd = max(0.0, min(10000.0, max_cost_per_month_usd))
    max_leads_per_month = max(1, min(100000, max_leads_per_month))

    async with async_session() as session:
        tenant_repo = TenantRepository(session)
        tenant = await tenant_repo.get_by_id(tenant_id)
        if tenant:
            config = tenant.config or {}
            config.update({
                "max_ai_requests_per_day": max_ai_requests_per_day,
                "max_tokens_per_day": max_tokens_per_day,
                "max_cost_per_month_usd": max_cost_per_month_usd,
                "max_leads_per_month": max_leads_per_month,
                "ai_enabled": ai_enabled,
            })
            await tenant_repo.update_config(tenant_id, config=config)
            await session.commit()

    return RedirectResponse("/billing?success=Billing settings updated", status_code=303)


async def _fire_webhooks(tenant_id: int, event: str, data: dict):
    """Fire webhooks for a given event."""
    import hashlib, hmac, json as _json
    import aiohttp
    try:
        async with async_session() as session:
            wh_repo = WebhookRepository(session, tenant_id)
            webhooks = await wh_repo.list_active()
            for wh in webhooks:
                if event not in (wh.events or []):
                    continue
                try:
                    payload = _json.dumps({"event": event, "tenant_id": tenant_id, "data": data}, default=str)
                    headers = {"Content-Type": "application/json"}
                    if wh.secret:
                        sig = hmac.new(wh.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
                        headers["X-Webhook-Signature"] = sig
                    async with aiohttp.ClientSession() as http:
                        resp = await http.post(wh.url, data=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10))
                        success = resp.status < 400
                    await wh_repo.mark_triggered(wh.id, success)
                except Exception as e:
                    logger.warning("Webhook %d failed: %s", wh.id, e)
                    await wh_repo.mark_triggered(wh.id, False)
            await session.commit()
    except Exception as e:
        logger.error("Webhook fire error for tenant %d: %s", tenant_id, e)


@app.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = MessageTemplateRepository(session, tenant_id)
        templates_list = await repo.list_active()
    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, templates=templates_list, csrf_token=csrf)
    return templates.TemplateResponse(request, "templates.html", ctx)


@app.post("/templates/add")
async def template_add(
    request: Request,
    name: str = Form(...),
    category: str = Form("general"),
    body: str = Form(...),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = MessageTemplateRepository(session, tenant_id)
        await repo.create(name=sanitize_input(name, 200), category=category, body=sanitize_input(body, 5000))
        await session.commit()
    return RedirectResponse("/templates?success=Шаблон+создан", status_code=303)


@app.post("/templates/{template_id}/delete")
async def template_delete(request: Request, template_id: int, csrf_token: str = Form("")):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = MessageTemplateRepository(session, tenant_id)
        tmpl = await repo.get_by_id(template_id)
        if tmpl:
            tmpl.is_active = False
            await session.commit()
    return RedirectResponse("/templates?success=Шаблон+удалён", status_code=303)


@app.post("/api/templates/generate")
async def api_template_generate(request: Request):
    user = await get_current_user(request)
    if not user:
        return HTMLResponse(content="Unauthorized", status_code=401)
    data = await request.json()
    category = data.get("category", "general")
    description = data.get("description", "")
    context = data.get("context", "")
    if not description.strip():
        return HTMLResponse(content="Описание обязательно", status_code=400)

    from config import settings as _settings
    api_key = _settings.openai_api_key or ""
    base_url = _settings.openai_base_url or "https://api.groq.com/openai/v1"
    model = _settings.openai_model or "llama-3.3-70b-versatile"
    if not api_key:
        return HTMLResponse(content="AI API ключ не настроен", status_code=500)

    category_labels = {
        "general": "общий",
        "first_contact": "проактивный первый контакт — мы сами пишем клиенту",
        "follow_up": "повторное напоминание / follow-up",
        "deal_close": "закрытие сделки / договорённость",
    }
    cat_label = category_labels.get(category, category)

    tenant_id = await _get_tenant_id(user)
    existing_examples = ""
    try:
        async with async_session() as session:
            repo = MessageTemplateRepository(session, tenant_id)
            all_tmpls = await repo.list_active()
            if all_tmpls:
                examples = []
                for t in all_tmpls[:8]:
                    examples.append(f"[{t.category}] {t.name}:\n{t.body[:300]}")
                existing_examples = "\n\nУЖЕ СУЩЕСТВУЮЩИЕ ШАБЛОНЫ (учти стиль при генерации новых):\n" + "\n---\n".join(examples)
    except Exception:
        pass

    prompt = (
        "Ты — копирайтер компании по ремонту квартир в Москве.\n\n"
        "ВАЖНО: Мы — компания по ремонту. Мы МОНИТОРИМ Telegram-чаты и находим людей, "
        "которые ищут ремонт или задают вопросы про ремонт. Мы ПЕРВЫЕ пишем им предложение услуг.\n"
        "Клиент НЕ обращался к нам — это МЫ выходим на него.\n\n"
        f"Категория: {cat_label}\n"
        f"Описание: {description}\n"
    )
    if context:
        prompt += f"Дополнительный контекст: {context}\n"
    prompt += existing_examples
    prompt += (
        "\n\nСтиль сообщений:\n"
        "- Дружелюбный, но профессиональный\n"
        "- Без спама и навязчивости\n"
        "- Конкретика по услугам, без воды\n"
        "- Ссылка на то, как нашли клиента (например: «видели ваш вопрос в чате»)\n"
        "- Призыв к действию (написать нам, узнать цену, выехать на замер)\n"
        "- Telegram-формат: короткие абзацы, без длинных простыней\n"
        "- Не начинай с «Здравствуйте, спасибо за обращение» — мы первые пишем!\n"
        "- Используй эмодзи умеренно\n\n"
        "Верни ТОЛЬКО JSON без markdown без кодбеков:\n"
        '{"name": "название шаблона на русском (до 50 символов)", '
        '"body": "текст шаблона на русском (1-5 сообщений, с переносами строк)"}'
    )

    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 1024,
                },
                timeout=_aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logging.error("Groq API error %s: %s", resp.status, err)
                    return HTMLResponse(content="Ошибка AI API", status_code=502)
                result = await resp.json()
                text = result["choices"][0]["message"]["content"].strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
                text = text.strip()
                parsed = json.loads(text, strict=False)
                return HTMLResponse(
                    content=json.dumps({"name": parsed.get("name", ""), "body": parsed.get("body", "")}),
                    media_type="application/json",
                )
    except Exception as e:
        logging.error("AI template generation error: %s", e)
        return HTMLResponse(content="Ошибка генерации шаблона", status_code=500)


@app.get("/kanban", response_class=HTMLResponse)
async def kanban_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        leads_repo = LeadRepository(session, tenant_id)
        all_leads = await leads_repo.list_all()
        active_leads = [l for l in all_leads if l.status not in ("deleted", "archive")]
    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, leads=active_leads, csrf_token=csrf)
    return templates.TemplateResponse(request, "kanban.html", ctx)


@app.post("/api/kanban/move")
async def kanban_move(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    lead_id = body.get("lead_id")
    new_status = body.get("status")
    if not lead_id or not new_status:
        raise HTTPException(status_code=400)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)
        if not lead:
            raise HTTPException(status_code=404)
        old_status = lead.status
        lead.status = new_status
        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead_id, user_id=user.id, action="status_change",
            old_value=old_status, new_value=new_status,
            note=f"{user.full_name or user.username} (канбан)",
        )
        await ActionLogRepository(session, tenant_id).log(
            user.id, "status_change", lead_id=lead_id,
            meta={"from": old_status, "to": new_status, "via": "kanban"},
        )
        await session.commit()
    await _fire_webhooks(tenant_id, "status_change", {"lead_id": lead_id, "from": old_status, "to": new_status})
    return {"ok": True}


@app.get("/webhooks", response_class=HTMLResponse)
async def webhooks_page(request: Request):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = WebhookRepository(session, tenant_id)
        wh_list = (await session.execute(select(Webhook).where(Webhook.tenant_id == tenant_id))).scalars().all()
    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, webhooks=wh_list, csrf_token=csrf)
    return templates.TemplateResponse(request, "webhooks.html", ctx)


@app.post("/webhooks/add")
async def webhook_add(
    request: Request,
    url: str = Form(...),
    events: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)
    tenant_id = await _get_tenant_id(user)
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    import secrets
    async with async_session() as session:
        repo = WebhookRepository(session, tenant_id)
        await repo.create(url=sanitize_input(url, 1024), events=event_list, secret=secrets.token_hex(32))
        await session.commit()
    return RedirectResponse("/webhooks?success=Вебхук+создан", status_code=303)


@app.post("/webhooks/{webhook_id}/delete")
async def webhook_delete(request: Request, webhook_id: int, csrf_token: str = Form("")):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = WebhookRepository(session, tenant_id)
        wh = await repo.get_by_id(webhook_id)
        if wh:
            wh.is_active = False
            await session.commit()
    return RedirectResponse("/webhooks?success=Вебхук+удалён", status_code=303)


@app.get("/api/log-profile-click/{lead_id}")
async def log_profile_click(request: Request, lead_id: int):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        await ActionLogRepository(session, tenant_id).log(
            user.id, "profile_click", lead_id=lead_id,
            meta={"ip": request.client.host if request.client else None},
        )
        await session.commit()
    raise HTTPException(status_code=200)
