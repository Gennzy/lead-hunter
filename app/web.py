import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from fastapi import FastAPI, Request, Form, Query, HTTPException, UploadFile, File, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_, update, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

# Rate limiter for login: {ip: [timestamp, ...]}
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 900  # 15 minutes
LOGIN_LOCKOUT_SECONDS = 1800  # 30 minutes

from app.models import Lead, User, LeadHistory, BlacklistedUser, Base, engine, async_session, Tenant, TelegramSession, MessageTemplate, Webhook
from app.repositories import (
    TenantRepository, UserRepository, LeadRepository,
    LeadHistoryRepository, BlacklistedUserRepository, TelegramSessionRepository,
    TenantUsageRepository, ActionLogRepository, MessageTemplateRepository, WebhookRepository,
    EmployeeTargetRepository, CommissionRepository, PenaltyRepository, WorkSessionRepository,
    ResponseTimeRepository, LeadSourceRepository, ManagerActionRepository, ManagerContractRepository,
    TheftDetectionRepository,
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
from config import settings, utcnow

logger = logging.getLogger(__name__)

CHATS_FILE = Path("config_chats.json")


def _load_chats() -> list[dict]:
    if CHATS_FILE.exists():
        data = json.loads(CHATS_FILE.read_text())
        if data and isinstance(data[0], str):
            data = [{"url": c, "active": True} for c in data]
            _save_chats(data)
        return data
    return []


def _save_chats(chats: list[dict]):
    CHATS_FILE.write_text(json.dumps(chats, ensure_ascii=False, indent=2))


async def _sync_tenant_chats(tenant_id):
    """Mirror active chats into tenant config so per-tenant monitors pick them up."""
    active_urls = [c["url"] for c in _load_chats() if c.get("active", True)]
    async with async_session() as session:
        repo = TenantRepository(session)
        tenant = await (repo.get_by_id(tenant_id) if tenant_id is not None
                        else repo.get_by_slug("default"))
        if not tenant:
            return
        cfg = dict(tenant.config or {})
        if active_urls:
            cfg["monitored_chats"] = ",".join(active_urls[:200])
        else:
            cfg.pop("monitored_chats", None)
        tenant.config = cfg
        flag_modified(tenant, "config")
        await session.commit()


def _get_active_chat_urls() -> list[str]:
    return [c["url"] for c in _load_chats() if c.get("active", True)]


templates = Jinja2Templates(directory="app/templates")

MSK = timezone(timedelta(hours=3))

def _to_msk(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK)

templates.env.filters["msk"] = _to_msk

def _time_ago(dt):
    if dt is None:
        return ""
    from datetime import datetime
    now = utcnow()
    if dt.tzinfo is None:
        pass
    else:
        dt = dt.replace(tzinfo=None)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "только что"
    elif seconds < 3600:
        mins = seconds // 60
        return f"{mins} мин назад"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours} ч назад"
    elif seconds < 604800:
        days = seconds // 86400
        return f"{days} дн назад"
    else:
        weeks = seconds // 604800
        return f"{weeks} нед назад"

templates.env.filters["time_ago"] = _time_ago

def _jinja_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0

templates.env.filters["int"] = _jinja_int


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
    from config import settings
    return {**theme, "user": user, "vapid_public_key": settings.vapid_public_key, **kwargs}


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


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    subscription = body.get("subscription")
    if not subscription:
        return JSONResponse({"error": "no subscription"}, status_code=400)

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        user_obj = await session.get(User, user.id)
        if user_obj:
            push_subs = user_obj.push_subscriptions or []
            endpoint = subscription.get("endpoint", "")
            push_subs = [s for s in push_subs if s.get("endpoint") != endpoint]
            push_subs.append(subscription)
            user_obj.push_subscriptions = push_subs[-3:]
            await session.commit()

    return JSONResponse({"ok": True})


@app.delete("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    endpoint = body.get("endpoint", "")

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        user_obj = await session.get(User, user.id)
        if user_obj and user_obj.push_subscriptions:
            user_obj.push_subscriptions = [s for s in user_obj.push_subscriptions if s.get("endpoint") != endpoint]
            await session.commit()

    return JSONResponse({"ok": True})


@app.get("/api/push/vapid-public-key")
async def push_vapid_key():
    from config import settings
    return JSONResponse({"key": settings.vapid_public_key})


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
    
    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(None, error=error, theme_color=theme_color, csrf_token=csrf)
    return templates.TemplateResponse(request, "login.html", ctx)


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    _login_attempts[client_ip] = [t for t in _login_attempts[client_ip] if now - t < LOGIN_WINDOW_SECONDS]
    if len(_login_attempts[client_ip]) >= LOGIN_MAX_ATTEMPTS:
        remaining = int(LOGIN_LOCKOUT_SECONDS - (now - _login_attempts[client_ip][0]))
        if remaining > 0:
            return RedirectResponse(f"/login?error=Слишком много попыток. Подождите {remaining//60} мин.", status_code=303)

    # CSRF validation
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/login?error=Ошибка CSRF. Попробуйте снова.", status_code=303)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        _login_attempts[client_ip].append(now)
        return RedirectResponse("/login?error=Неверный логин или пароль", status_code=303)

    # Clear rate limit on success
    _login_attempts.pop(client_ip, None)

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
    period = request.query_params.get("period", "week")

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        now = utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        month_start = now - timedelta(days=30)

        if period == "today":
            period_start = today_start
        elif period == "month":
            period_start = month_start
        elif period == "week":
            period_start = week_start
        else:
            period_start = None
            period = "all"

        total_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            total_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            total_filters.append(manager_filter)
        if period_start:
            total_filters.append(Lead.created_at >= period_start)
        total = (await session.execute(
            select(func.count(Lead.id)).where(*total_filters)
        )).scalar() or 0

        new_today = await leads.count_today(today_start)
        new_today_quality = await leads.count_today_quality(today_start)
        new_week = await leads.count_this_week(week_start)

        status_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            status_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            status_filters.append(manager_filter)
        if period_start:
            status_filters.append(Lead.created_at >= period_start)
        status_rows = (await session.execute(
            select(Lead.status, func.count(Lead.id))
            .where(*status_filters)
            .group_by(Lead.status)
        )).fetchall()
        status_counts = {s: c for s, c in status_rows}

        # Top chats
        chat_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            chat_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            chat_filters.append(manager_filter)
        if period_start:
            chat_filters.append(Lead.created_at >= period_start)
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
        if period_start:
            recent_filters.append(Lead.created_at >= period_start)
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

        def _pct(cur, prev):
            if prev:
                return round((cur - prev) / prev * 100)
            return None if not cur else 100

        async def _cnt(filters):
            return (await session.execute(
                select(func.count(Lead.id)).where(*filters))).scalar() or 0

        # === Deltas vs previous period ===
        deltas = {}
        if period_start:
            duration = now - period_start
            prev_start = period_start - duration
            base_prev = [Lead.created_at >= prev_start, Lead.created_at < period_start,
                         Lead.status != "deleted"]
            if tenant_id is not None:
                base_prev.append(Lead.tenant_id == tenant_id)
            if manager_filter:
                base_prev.append(manager_filter)
            total_prev = await _cnt(base_prev)
            deltas["total"] = _pct(total, total_prev)
            deals_prev = await _cnt(base_prev + [Lead.status == "deal"])
            deltas["deal"] = _pct(status_counts.get("deal", 0), deals_prev)

        # Quality-today delta vs yesterday
        y_start = today_start - timedelta(days=1)
        quality_base = [Lead.lead_score >= 80, Lead.status != "deleted"]
        if tenant_id is not None:
            quality_base.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            quality_base.append(manager_filter)
        quality_prev = await _cnt(quality_base + [
            Lead.created_at >= y_start, Lead.created_at < today_start])
        deltas["quality"] = _pct(new_today_quality, quality_prev)

        # Processed-today delta vs yesterday (by update time)
        processed_base = [Lead.status.in_(["contacted", "in_progress", "deal", "interested"])]
        if tenant_id is not None:
            processed_base.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            processed_base.append(manager_filter)
        processed_prev = await _cnt(processed_base + [
            Lead.updated_at >= y_start, Lead.updated_at < today_start])
        deltas["processed"] = _pct(processed_today, processed_prev)

        # === Daily leads chart (last 30 days) ===
        chart_filters = [Lead.created_at >= now - timedelta(days=30)]
        if tenant_id is not None:
            chart_filters.append(Lead.tenant_id == tenant_id)
        if manager_filter:
            chart_filters.append(manager_filter)
        day_rows = (await session.execute(
            select(func.date(Lead.created_at), func.count(Lead.id))
            .where(*chart_filters).group_by(func.date(Lead.created_at))
        )).fetchall()
        counts_by_day = {str(d): int(c or 0) for d, c in day_rows}
        daily_leads = []
        for i in range(29, -1, -1):
            day = (now - timedelta(days=i)).date()
            cnt = counts_by_day.get(day.isoformat(), 0)
            daily_leads.append({
                "label": day.strftime('%d.%m'),
                "count": cnt,
                "weekend": day.weekday() >= 5,
            })
        chart_max = max((d["count"] for d in daily_leads), default=0)
        chart_total = sum(d["count"] for d in daily_leads)

        # === Pending chat suggestions badge (admins only) ===
        pending_chats = 0
        if user.role in ("super_admin", "admin"):
            from app.models import ChatSuggestion
            cs_filters = [ChatSuggestion.status == "pending"]
            if tenant_id is not None:
                cs_filters.append(ChatSuggestion.tenant_id == tenant_id)
            pending_chats = (await session.execute(
                select(func.count(ChatSuggestion.id)).where(*cs_filters)
            )).scalar() or 0

        # === Team activity feed (last actions) ===
        from app.models import ActionLog
        act_filters = []
        if tenant_id is not None:
            act_filters.append(ActionLog.tenant_id == tenant_id)
        if user.role == "manager":
            act_filters.append(ActionLog.user_id == user.id)
        act_rows = (await session.execute(
            select(ActionLog, User)
            .outerjoin(User, ActionLog.user_id == User.id)
            .where(*act_filters)
            .order_by(ActionLog.created_at.desc()).limit(12)
        )).fetchall()
        _action_labels = {
            "login": "вошёл в систему",
            "lead_view": "открыл карточку лида",
            "click_write": "написал клиенту",
            "profile_click": "открыл профиль клиента",
            "status_change": "сменил статус",
            "mark_not_lead": "отметил «не лид»",
            "csv_export": "экспортировал лиды",
        }
        team_activity = []
        for a, u in act_rows:
            label = _action_labels.get(a.action_type, a.action_type)
            meta = a.meta if isinstance(a.meta, dict) else {}
            if a.action_type == "status_change" and meta:
                label += f": {meta.get('from', '?')} → {meta.get('to', '?')}"
            team_activity.append({
                "user": (u.full_name or u.username) if u else "Система",
                "text": label,
                "lead_id": a.lead_id,
                "at": a.created_at,
            })

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, total=total, new_today=new_today, new_today_quality=new_today_quality, new_week=new_week, status_counts=status_counts, chat_leads=chat_leads, recent_leads=recent_leads, hot_leads=hot_leads, attention_leads=attention_leads, unprocessed_count=unprocessed_count, processed_today=processed_today, overdue_count=overdue_count, csrf_token=csrf, now=utcnow(), period=period, deltas=deltas, daily_leads=daily_leads, chart_max=chart_max, chart_total=chart_total, pending_chats=pending_chats, team_activity=team_activity)
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
    score_min: int = Query(None, ge=0, le=100),
    urgency_f: str = Query(None, alias="urgency"),
    manager_f: str = Query(None, alias="manager"),
    noresp: int = Query(None, ge=1, le=90),
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
        if score_min is not None:
            extra_filters.append(Lead.lead_score >= score_min)
        if urgency_f in ("high", "medium", "low"):
            extra_filters.append(Lead.urgency == urgency_f)
        if user.role != "manager" and manager_f:
            if manager_f == "none":
                extra_filters.append(Lead.assigned_to.is_(None))
            elif manager_f.isdigit():
                extra_filters.append(Lead.assigned_to == int(manager_f))
        if noresp:
            from sqlalchemy import and_ as sa_and, or_ as sa_or
            nr_cutoff = utcnow() - timedelta(days=noresp)
            extra_filters.append(sa_and_(
                sa_or_(Lead.last_responded_at.is_(None), Lead.last_responded_at < nr_cutoff),
                Lead.created_at <= nr_cutoff,
            ))

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

        # New leads this week
        week_start = utcnow() - timedelta(days=utcnow().weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        nw_filters = [Lead.created_at >= week_start, Lead.status != "deleted"]
        if tenant_id is not None:
            nw_filters.append(Lead.tenant_id == tenant_id)
        if manager_id:
            nw_filters.append(Lead.assigned_to == manager_id)
        new_week = (await session.execute(select(func.count(Lead.id)).where(*nw_filters))).scalar() or 0

        # Repeat-lead badges: other leads from the same authors (page scope)
        repeat_info = {}
        page_user_ids = [l.user_id for l in all_leads if l.user_id]
        if page_user_ids:
            rp_rows = (await session.execute(
                select(Lead.id, Lead.user_id)
                .where(Lead.user_id.in_(page_user_ids), Lead.status != "deleted")
            )).fetchall()
            by_user = {}
            for rid, uid in rp_rows:
                by_user.setdefault(uid, []).append(rid)
            for l in all_leads:
                if l.user_id and len(by_user.get(l.user_id, [])) > 1:
                    others = [rid for rid in by_user[l.user_id] if rid != l.id]
                    repeat_info[l.id] = {"other_id": others[0], "count": len(others)}

        # Managers list for admin filter
        managers_list = []
        if user.role in ("super_admin", "admin"):
            from app.repositories import UserRepository
            users_all = await UserRepository(session, tenant_id).list_active()
            managers_list = [{"id": u.id, "name": (u.full_name or u.username or "")[:22]}
                             for u in users_all if u.role in ("admin", "manager")]

    # Prebuilt query strings (preserve filters across tabs/sort/csv)
    from urllib.parse import urlencode
    qs_parts = []
    if chat:
        qs_parts.append(("chat", chat))
    if search:
        qs_parts.append(("search", search))
    if date_from:
        qs_parts.append(("date_from", date_from))
    if date_to:
        qs_parts.append(("date_to", date_to))
    if score_min is not None:
        qs_parts.append(("score_min", score_min))
    if urgency_f in ("high", "medium", "low"):
        qs_parts.append(("urgency", urgency_f))
    if user.role != "manager" and manager_f:
        qs_parts.append(("manager", manager_f))
    if noresp:
        qs_parts.append(("noresp", noresp))
    qs_nostatus = ("&" + urlencode(qs_parts)) if qs_parts else ""
    csv_qs = urlencode((([("status", lead_status)] if lead_status else []) + qs_parts))

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, leads=all_leads, total=total, page=page, total_pages=total_pages, status_filter=lead_status or "", chat_filter=chat or "", search=search or "", date_from=date_from or "", date_to=date_to or "", sort=sort or "", status_counts=status_counts, available_chats=available_chats, csrf_token=csrf, now=utcnow(), new_week=new_week, repeat_info=repeat_info, managers_list=managers_list, score_min=score_min, urgency_f=urgency_f or "", manager_f=manager_f or "", noresp=noresp, qs_nostatus=qs_nostatus, csv_qs=csv_qs)
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
        headers={"Content-Disposition": f"attachment; filename=leads_{utcnow().strftime('%Y%m%d_%H%M%S')}.csv"},
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
        await session.commit()

        # Record first response time (when assigned user views the lead)
        if lead.assigned_to == user.id:
            from app.repositories import ResponseTimeRepository
            resp_repo = ResponseTimeRepository(session, tenant_id)
            await resp_repo.record_response(lead_id)

        assignee = None
        if lead.assigned_to:
            users = UserRepository(session, tenant_id)
            assignee = await users.get_by_id(lead.assigned_to)

        history_repo = LeadHistoryRepository(session, tenant_id)
        history = await history_repo.list_for_lead(lead_id)

        prev_id, next_id = await leads.get_prev_next(lead_id)

        from app.models import Appointment, FollowUp
        from sqlalchemy import select as sel
        appointments = (await session.execute(
            sel(Appointment).where(Appointment.lead_id == lead_id).order_by(Appointment.scheduled_at.desc())
        )).scalars().all()
        follow_ups = (await session.execute(
            sel(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.status == "pending").order_by(FollowUp.scheduled_at.asc())
        )).scalars().all()

        # Author's other messages across chats (context: what else they wrote)
        author_messages = []
        if lead.user_id:
            from app.models import UserMessageHistory
            author_messages = (await session.execute(
                sel(UserMessageHistory)
                .where(UserMessageHistory.tenant_id == lead.tenant_id,
                       UserMessageHistory.user_id == lead.user_id)
                .order_by(UserMessageHistory.created_at.desc()).limit(15)
            )).scalars().all()

        lead_data = {
            "id": lead.id, "user_id": lead.user_id, "username": lead.username,
            "first_name": lead.first_name, "last_name": lead.last_name,
            "profile_link": lead.profile_link, "message_text": lead.message_text,
            "reply_to_id": lead.reply_to_id, "reply_to_text": lead.reply_to_text,
            "chat_title": lead.chat_title, "chat_username": lead.chat_username,
            "message_id": lead.message_id, "lead_score": lead.lead_score,
            "urgency": lead.urgency, "reason": lead.reason,
            "recommended_message": lead.recommended_message, "status": lead.status,
            "assigned_to": lead.assigned_to, "phone": lead.phone,
            "deal_amount": lead.deal_amount, "deal_currency": lead.deal_currency,
            "deal_closed_at": lead.deal_closed_at, "is_notified": lead.is_notified,
            "feedback": lead.feedback, "feedback_reason": lead.feedback_reason,
            "last_responded_at": lead.last_responded_at, "created_at": lead.created_at,
            "updated_at": lead.updated_at,
            "hotness": lead.hotness, "ai_summary": lead.ai_summary,
            "next_action": lead.next_action, "budget": lead.budget,
            "timeline": lead.timeline, "readiness": lead.readiness,
        }

    from app.ml_scorer import score_lead
    ml_prediction = await score_lead(lead, tenant_id)

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, lead=lead, assignee=assignee, history=history, prev_id=prev_id, next_id=next_id, csrf_token=csrf, error=error, ml_prediction=ml_prediction, appointments=appointments, follow_ups=follow_ups, author_messages=author_messages)
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
        if user.role == "manager" and lead.assigned_to != user.id:
            return RedirectResponse(f"/leads/{lead_id}", status_code=303)

        if feedback in ("useful", "not_useful"):
            is_new_feedback = lead.feedback != feedback
            lead.feedback = feedback
            lead.feedback_reason = reason if reason else None
            await session.commit()

            # Train ML on feedback only if feedback changed
            if is_new_feedback:
                try:
                    from app.ml_scorer import train_on_outcome
                    if feedback == "useful":
                        train_on_outcome(lead, is_positive=True, learning_rate=0.03)
                    else:
                        train_on_outcome(lead, is_positive=False, learning_rate=0.03)
                except Exception:
                    pass

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
    deal_amount: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    valid_statuses = {"new", "contacted", "interested", "not_interested", "deal", "archive", "deleted", "missed_call", "in_progress"}
    if lead_status not in valid_statuses:
        return HTMLResponse(content="Invalid status", status_code=400)

    if lead_status == "deal" and not deal_comment.strip():
        return RedirectResponse(f"/leads/{lead_id}?error=Для статуса «Сделка» обязателен комментарий", status_code=303)

    if lead_status == "not_interested" and not deal_comment.strip():
        return RedirectResponse(f"/leads/{lead_id}?error=Для статуса «Не интересует» обязателена причина", status_code=303)

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

        if lead_status == "not_interested" and deal_comment.strip():
            lead.feedback_reason = deal_comment.strip()[:50]

        if lead_status == "deal" and deal_amount.strip():
            try:
                lead.deal_amount = float(deal_amount.strip().replace(" ", ""))
                lead.deal_closed_at = utcnow()
            except (ValueError, TypeError):
                pass

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
            meta={"old": old_status, "new": lead_status},
        )

        # Record response time when manager first contacts the lead
        if lead_status == "contacted" and old_status == "new":
            try:
                from app.repositories import ResponseTimeRepository
                resp_repo = ResponseTimeRepository(session, tenant_id)
                await resp_repo.record_response(lead_id)
            except Exception:
                pass

        await session.commit()

        # Train ML on outcome
        try:
            from app.ml_scorer import train_on_outcome
            if lead_status == "deal":
                lead.feedback = "useful"
                train_on_outcome(lead, is_positive=True)
            elif lead_status in ("not_interested", "deleted"):
                lead.feedback = "not_useful"
                train_on_outcome(lead, is_positive=False)
        except Exception:
            pass
    await _fire_webhooks(tenant_id, "status_change", {"lead_id": lead_id, "from": old_status, "to": lead_status})
    if lead_status == "deal":
        await _fire_webhooks(tenant_id, "deal_closed", {"lead_id": lead_id, "score": lead.lead_score if lead else 0})
    return RedirectResponse(f"/leads/{lead_id}?success=Статус+изменён", status_code=303)


@app.post("/leads/{lead_id}/phone")
async def update_phone(
    request: Request,
    lead_id: int,
    phone: str = Form(""),
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

        lead.phone = phone.strip()[:20] if phone.strip() else None

        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead_id,
            user_id=user.id,
            action="phone_update",
            note=f"Телефон: {lead.phone or 'удалён'}",
        )
        await session.commit()

    return RedirectResponse(f"/leads/{lead_id}?success=Телефон+обновлён", status_code=303)


@app.post("/leads/{lead_id}/appointment")
async def create_appointment(
    request: Request,
    lead_id: int,
    scheduled_at: str = Form(...),
    title: str = Form(""),
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

        from app.repositories import AppointmentRepository
        from datetime import datetime

        try:
            dt = datetime.fromisoformat(scheduled_at)
        except ValueError:
            return RedirectResponse(f"/leads/{lead_id}?error=Неверная+дата", status_code=303)

        apt_repo = AppointmentRepository(session, tenant_id)
        await apt_repo.create(
            lead_id=lead_id,
            user_id=user.id,
            title=title or f"Замер — {lead.first_name or ''} {lead.last_name or ''}".strip(),
            scheduled_at=dt,
        )

        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead_id,
            user_id=user.id,
            action="appointment_created",
            note=f"Запланировано: {title} на {dt.strftime('%d.%m.%Y %H:%M')}",
        )
        await session.commit()

    return RedirectResponse(f"/leads/{lead_id}?success=Встреча+запланирована", status_code=303)


@app.post("/appointments/{apt_id}/complete")
async def complete_appointment(
    request: Request,
    apt_id: int,
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        from app.repositories import AppointmentRepository
        apt_repo = AppointmentRepository(session, tenant_id)
        apt = await apt_repo.get_by_id(apt_id)

        if not apt:
            return RedirectResponse("/leads", status_code=303)

        if user.role == "manager":
            lead_obj = await LeadRepository(session, tenant_id).get_by_id(apt.lead_id)
            if not lead_obj or lead_obj.assigned_to != user.id:
                return RedirectResponse("/leads", status_code=303)

        await apt_repo.complete(apt_id)

        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=apt.lead_id,
            user_id=user.id,
            action="appointment_completed",
            note=f"Встреча завершена: {apt.title}",
        )
        await session.commit()

    return RedirectResponse(f"/leads/{apt.lead_id}?success=Встреча+завершена", status_code=303)


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
        elif action in ("new", "contacted", "interested", "not_interested", "deal", "archive", "deleted", "missed_call", "in_progress"):
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

        # Record response time tracking
        from app.repositories import ResponseTimeRepository
        resp_repo = ResponseTimeRepository(session, tenant_id)
        await resp_repo.record_assignment(assigned_to, lead_id)

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
async def analytics(request: Request, days: int = Query(30, ge=1, le=365)):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        return RedirectResponse("/", status_code=303)

    tenant_id = await _get_tenant_id(user)
    days_start = utcnow() - timedelta(days=days)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        data = await leads.get_analytics()

        # Funnel data
        funnel_statuses = ["new", "contacted", "interested", "deal"]
        funnel_filters = [Lead.created_at >= days_start]
        if tenant_id is not None:
            funnel_filters.append(Lead.tenant_id == tenant_id)
        funnel_q = select(Lead.status, func.count(Lead.id)).where(*funnel_filters).group_by(Lead.status)
        funnel_rows = (await session.execute(funnel_q)).fetchall()
        funnel_map = {s: c for s, c in funnel_rows}
        funnel = [(s, funnel_map.get(s, 0)) for s in funnel_statuses]

        # Source report: top chats by deal conversion
        source_filters = [Lead.status != "deleted", Lead.created_at >= days_start]
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

            # Revenue from this source
            rev_filters = [Lead.chat_title == chat_title, Lead.deal_amount.isnot(None)]
            if tenant_id is not None:
                rev_filters.append(Lead.tenant_id == tenant_id)
            revenue = (await session.execute(select(func.sum(Lead.deal_amount)).where(*rev_filters))).scalar() or 0

            source_data.append({"chat": chat_title, "total": count, "deals": deal_count, "avg_score": round(avg_score or 0, 1), "conversion": round(conv, 1), "revenue": revenue})

    # Period comparison: this week vs last week
    now = utcnow()
    this_week_start = now - timedelta(days=7)
    last_week_start = now - timedelta(days=14)

    # This week stats
    tw_filters = [Lead.status != "deleted", Lead.created_at >= this_week_start]
    if tenant_id is not None:
        tw_filters.append(Lead.tenant_id == tenant_id)
    this_week_leads = (await session.execute(select(func.count(Lead.id)).where(*tw_filters))).scalar() or 0
    this_week_deals = (await session.execute(select(func.count(Lead.id)).where(*tw_filters, Lead.status == "deal"))).scalar() or 0
    this_week_avg_result = (await session.execute(select(func.avg(Lead.lead_score)).where(*tw_filters))).scalar()
    this_week_avg_score = round(this_week_avg_result, 1) if this_week_avg_result else 0

    # Last week stats
    lw_filters = [Lead.status != "deleted", Lead.created_at >= last_week_start, Lead.created_at < this_week_start]
    if tenant_id is not None:
        lw_filters.append(Lead.tenant_id == tenant_id)
    last_week_leads = (await session.execute(select(func.count(Lead.id)).where(*lw_filters))).scalar() or 0
    last_week_deals = (await session.execute(select(func.count(Lead.id)).where(*lw_filters, Lead.status == "deal"))).scalar() or 0
    last_week_avg_result = (await session.execute(select(func.avg(Lead.lead_score)).where(*lw_filters))).scalar()
    last_week_avg_score = round(last_week_avg_result, 1) if last_week_avg_result else 0

    # Leads by day (last 7 days)
    leads_by_day = []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_next = day + timedelta(days=1)
        day_filters = [Lead.status != "deleted", Lead.created_at >= day, Lead.created_at < day_next]
        if tenant_id is not None:
            day_filters.append(Lead.tenant_id == tenant_id)
        day_count = (await session.execute(select(func.count(Lead.id)).where(*day_filters))).scalar() or 0
        leads_by_day.append((day.strftime("%d.%m"), day_count))

    # Get feedback analysis
    from app.analyzer import analyze_feedback
    feedback_data = await analyze_feedback(tenant_id)

    # Get prediction stats (cached 10 min — score_lead per lead is heavy)
    global _prediction_cache
    prediction_stats = None
    now_ts = time.time()
    if (_prediction_cache["data"] is not None and _prediction_cache["tenant"] == tenant_id
            and now_ts - _prediction_cache["ts"] < 600):
        prediction_stats = _prediction_cache["data"]
    if prediction_stats is None:
        try:
            from app.ml_scorer import score_lead
            from app.models import Lead as LeadModel
            pred_q = select(LeadModel).where(
                LeadModel.status.notin_(["deleted", "archive"]),
                LeadModel.lead_score.isnot(None),
            )
            if tenant_id:
                pred_q = pred_q.where(LeadModel.tenant_id == tenant_id)
            pred_leads = (await session.execute(pred_q.limit(100))).scalars().all()

            if pred_leads:
                high_prob = 0
                medium_prob = 0
                low_prob = 0
                total_prob = 0

                for lead in pred_leads:
                    pred = await score_lead(lead, tenant_id)
                    prob = pred.get("probability", 0)
                    total_prob += prob
                    if prob >= 70:
                        high_prob += 1
                    elif prob >= 40:
                        medium_prob += 1
                    else:
                        low_prob += 1

                prediction_stats = {
                    "high_probability": high_prob,
                    "medium_probability": medium_prob,
                    "low_probability": low_prob,
                    "avg_probability": round(total_prob / len(pred_leads), 1) if pred_leads else 0,
                }
                _prediction_cache.update({"data": prediction_stats, "ts": now_ts, "tenant": tenant_id})
        except Exception:
            pass

    hotness_stats = {"hot": 0, "warm": 0, "cold": 0, "total": 0}
    try:
        from sqlalchemy import func as sa_func
        hot_q = select(Lead.hotness, sa_func.count(Lead.id)).where(
            Lead.status.notin_(["deleted", "archive"])
        )
        if tenant_id:
            hot_q = hot_q.where(Lead.tenant_id == tenant_id)
        hot_q = hot_q.group_by(Lead.hotness)
        hot_rows = (await session.execute(hot_q)).all()
        for h, c in hot_rows:
            if h in ("hot", "warm", "cold"):
                hotness_stats[h] = c
                hotness_stats["total"] += c
    except Exception:
        pass

    ctx = await _template_ctx(user,
        by_chat=data["by_chat"], by_status=data["by_status"], avg_score=data["avg_score"],
        high_score=data["high_score"], medium_score=data["medium_score"], total=data["total_leads"],
        feedback_data=feedback_data, funnel=funnel, source_data=source_data,
        this_week_leads=this_week_leads, last_week_leads=last_week_leads,
        this_week_deals=this_week_deals, last_week_deals=last_week_deals,
        this_week_avg_score=this_week_avg_score, last_week_avg_score=last_week_avg_score,
        leads_by_day=leads_by_day, prediction_stats=prediction_stats,
        hotness_stats=hotness_stats, days=days,
    )
    return templates.TemplateResponse(request, "analytics.html", ctx)


@app.get("/analytics/export/csv")
async def analytics_export_csv(request: Request, days: int = Query(30, ge=1, le=365)):
    """CSV: funnel + source conversion report."""
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)
    days_start = utcnow() - timedelta(days=days)

    async with async_session() as session:
        filters = [Lead.status != "deleted", Lead.created_at >= days_start]
        if tenant_id is not None:
            filters.append(Lead.tenant_id == tenant_id)
        rows = (await session.execute(
            select(Lead.chat_title, func.count(Lead.id),
                   func.sum(case((Lead.status == "deal", 1), else_=0)),
                   func.coalesce(func.sum(case((Lead.status == "deal", Lead.deal_amount), else_=None)), 0))
            .where(*filters).group_by(Lead.chat_title)
            .order_by(func.count(Lead.id).desc())
        )).fetchall()

    import csv as _csv
    from io import StringIO
    buf = StringIO()
    w = _csv.writer(buf)
    w.writerow(["Чат", "Лидов", "Сделок", "Конверсия %", "Сумма сделок"])
    for title, cnt, deals, sm in rows:
        conv = round((deals / cnt * 100), 1) if cnt else 0
        w.writerow([title, cnt, int(deals or 0), conv, float(sm or 0)])
    buf.seek(0)
    from fastapi.responses import Response as _Resp
    return _Resp(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=analytics_{days}d.csv"},
    )


@app.get("/analytics/pdf")
async def analytics_pdf(request: Request):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
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

        # Source report
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

        # Status distribution
        status_filters = []
        if tenant_id is not None:
            status_filters.append(Lead.tenant_id == tenant_id)
        status_q = select(Lead.status, func.count(Lead.id)).where(*status_filters).group_by(Lead.status)
        status_rows = (await session.execute(status_q)).fetchall()
        status_dist = {s: c for s, c in status_rows}

    from fpdf import FPDF
    import io

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Аналитика — Lead Hunter", ln=True, align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, f"Сформировано: {utcnow().strftime('%d.%m.%Y %H:%M')} UTC", ln=True, align="C")
    pdf.ln(8)

    # Summary stats
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Сводка", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"Всего лидов: {data['total_leads']}", ln=True)
    pdf.cell(0, 7, f"Средний Lead Score: {data['avg_score']}", ln=True)
    pdf.cell(0, 7, f"Горячие лиды (90+): {data['high_score']}", ln=True)
    pdf.cell(0, 7, f"Тёплые лиды (70-89): {data['medium_score']}", ln=True)
    pdf.ln(5)

    # Sales funnel
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Воронка продаж", ln=True)
    pdf.set_font("Helvetica", "", 10)
    funnel_labels = {"new": "Новые", "contacted": "Контакт", "interested": "Заинтересованы", "deal": "Сделки"}
    for status, count in funnel:
        pdf.cell(0, 7, f"{funnel_labels.get(status, status)}: {count}", ln=True)
    pdf.ln(5)

    # Source report table
    if source_data:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Отчёт по источникам", ln=True)
        col_w = [60, 20, 20, 25, 25]
        headers = ["Чат", "Лидов", "Сделок", "Ср. Score", "Конверсия"]
        pdf.set_font("Helvetica", "B", 9)
        for i, h in enumerate(headers):
            pdf.cell(col_w[i], 7, h, border=1, align="C")
        pdf.ln()
        pdf.set_font("Helvetica", "", 9)
        for s in source_data:
            vals = [s["chat"][:30], str(s["total"]), str(s["deals"]), str(s["avg_score"]), f"{s['conversion']}%"]
            for i, v in enumerate(vals):
                pdf.cell(col_w[i], 7, v, border=1, align="C" if i > 0 else "L")
            pdf.ln()
        pdf.ln(5)

    # Status distribution
    if status_dist:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Распределение по статусам", ln=True)
        status_labels = {"new": "Новые", "contacted": "Контакт", "interested": "Заинтересованы",
                         "not_interested": "Не интересуют", "deal": "Сделки", "archive": "Архив", "deleted": "Удалённые"}
        pdf.set_font("Helvetica", "", 10)
        for status, count in sorted(status_dist.items(), key=lambda x: -x[1]):
            pdf.cell(0, 7, f"{status_labels.get(status, status)}: {count}", ln=True)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)

    filename = f"analytics_{utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

            # Conversion rate
            conversion = round(deals / leads_total * 100, 1) if leads_total > 0 else 0

            # Revenue from deals
            revenue = (await session.execute(
                select(func.sum(Lead.deal_amount)).where(
                    Lead.assigned_to == u.id,
                    Lead.deal_amount.isnot(None),
                    Lead.tenant_id == tenant_id if tenant_id else True,
                )
            )).scalar() or 0

            # Average response time
            from app.models import ResponseTimeLog
            avg_response = (await session.execute(
                select(func.avg(ResponseTimeLog.response_seconds)).where(
                    ResponseTimeLog.user_id == u.id,
                    ResponseTimeLog.response_seconds.isnot(None),
                    ResponseTimeLog.tenant_id == tenant_id if tenant_id else True,
                )
            )).scalar() or 0
            avg_response_minutes = round(avg_response / 60, 0) if avg_response else None

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
                "conversion": conversion,
                "revenue": revenue,
                "avg_response_minutes": int(avg_response_minutes) if avg_response_minutes else None,
            })

        total_team_leads = sum(d["leads_total"] for d in team_data)
        total_deals = sum(d["deals"] for d in team_data)

        # Average response time across team
        avg_response_time = "—"
        if team_data:
            times = [d["avg_response_minutes"] for d in team_data if d["avg_response_minutes"]]
            if times:
                avg_response_time = f"{round(sum(times)/len(times))}мин"

        # Leaderboard (top by deals)
        leaderboard = sorted(team_data, key=lambda x: (x["deals"], x["conversion"]), reverse=True)[:5]
        leaderboard = [{"name": d["user"].full_name or d["user"].username, "deals": d["deals"], "conversion": d["conversion"], "revenue": d["revenue"]} for d in leaderboard if d["deals"] > 0]

    ctx = await _template_ctx(user, team_data=team_data, total_team_leads=total_team_leads, total_deals=total_deals, avg_response_time=avg_response_time, leaderboard=leaderboard)
    return templates.TemplateResponse(request, "team.html", ctx)


@app.get("/team/kpi", response_class=HTMLResponse)
async def team_kpi_page(request: Request, period: str = Query(None)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    today = utcnow()
    current_period = period if (period and len(period) == 7 and period[4] == "-") else today.strftime("%Y-%m")

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()

        targets_repo = EmployeeTargetRepository(session, tenant_id)
        leads_repo = LeadRepository(session, tenant_id)

        kpi_data = []
        y_p, m_p = int(current_period[:4]), int(current_period[5:7])
        period_start = datetime(y_p, m_p, 1)
        period_end = datetime(y_p + 1, 1, 1) if m_p == 12 else datetime(y_p, m_p + 1, 1)
        for u in active_users:
            # Get actual stats for this period

            actual_leads = await leads_repo.count(
                Lead.assigned_to == u.id,
                Lead.created_at >= period_start,
                Lead.created_at < period_end,
            )
            actual_deals = await leads_repo.count(
                Lead.assigned_to == u.id,
                Lead.status == "deal",
                Lead.created_at >= period_start,
                Lead.created_at < period_end,
            )
            revenue_filters = [Lead.assigned_to == u.id,
                               Lead.deal_amount.isnot(None),
                               Lead.status == "deal",
                               Lead.created_at >= period_start,
                               Lead.created_at < period_end]
            if tenant_id is not None:
                revenue_filters.append(Lead.tenant_id == tenant_id)
            revenue_result = await session.execute(
                select(func.sum(Lead.deal_amount)).where(*revenue_filters)
            )
            actual_revenue = revenue_result.scalar() or 0

            target = await targets_repo.get_or_create(u.id, current_period)
            await targets_repo.update_actuals(u.id, current_period, actual_leads, actual_deals, actual_revenue)

            kpi_data.append({
                "user": u,
                "target": target,
                "leads_pct": round(actual_leads / target.target_leads * 100) if target.target_leads > 0 else 0,
                "deals_pct": round(actual_deals / target.target_deals * 100) if target.target_deals > 0 else 0,
                "revenue_pct": round(actual_revenue / target.target_revenue * 100) if target.target_revenue > 0 else 0,
            })

        await session.commit()

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, kpi_data=kpi_data, current_period=current_period, csrf_token=csrf)
    return templates.TemplateResponse(request, "team_kpi.html", ctx)


@app.post("/team/kpi/set-target")
async def team_kpi_set_target(
    request: Request,
    user_id: int = Form(...),
    period: str = Form(...),
    target_leads: int = Form(0),
    target_deals: int = Form(0),
    target_revenue: float = Form(0),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = EmployeeTargetRepository(session, tenant_id)
        target = await repo.get_or_create(user_id, period)
        target.target_leads = target_leads
        target.target_deals = target_deals
        target.target_revenue = target_revenue
        await session.commit()

    return RedirectResponse("/team/kpi?success=Цель+установлена", status_code=303)


@app.get("/team/kpi/export/csv")
async def team_kpi_export_csv(request: Request, period: str = Query(None)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    if not period:
        period = utcnow().strftime("%Y-%m")

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()
        user_map = {u.id: u for u in active_users}

        leads_repo = LeadRepository(session, tenant_id)
        from sqlalchemy import func as sa_func
        period_start = datetime.strptime(f"{period}-01", "%Y-%m-%d")
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1)

        rows = []
        for u in active_users:
            if u.role not in ("admin", "manager"):
                continue
            leads_count = await leads_repo.count(
                Lead.assigned_to == u.id,
                Lead.created_at >= period_start,
                Lead.created_at < period_end,
                Lead.status != "deleted",
            )
            deals_count = await leads_repo.count(
                Lead.assigned_to == u.id,
                Lead.created_at >= period_start,
                Lead.created_at < period_end,
                Lead.status == "deal",
            )
            rows.append({
                "user": u.full_name or u.username,
                "leads": leads_count,
                "deals": deals_count,
                "conversion": f"{(deals_count/leads_count*100):.1f}%" if leads_count > 0 else "0%",
            })

    import csv, io
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["user", "leads", "deals", "conversion"])
    writer.writeheader()
    writer.writerows(rows)

    from starlette.responses import StreamingResponse
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=kpi_{period}.csv"},
    )


@app.get("/team/commissions", response_class=HTMLResponse)
async def team_commissions_page(request: Request, period: str = Query(None)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    if not period:
        period = utcnow().strftime("%Y-%m")

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()
        user_map = {u.id: u for u in active_users}

        comm_repo = CommissionRepository(session, tenant_id)
        summary = await comm_repo.get_summary(period)
        all_comms = await comm_repo.list_all(period)

        penalty_repo = PenaltyRepository(session, tenant_id)

        comm_data = []
        for row in summary:
            uid, total_comm, total_bonus, total_deals, deals_count = row
            penalties_total = await penalty_repo.get_total(uid, period)
            net = (total_comm or 0) + (total_bonus or 0) - penalties_total
            comm_data.append({
                "user": user_map.get(uid),
                "total_commission": total_comm or 0,
                "total_bonus": total_bonus or 0,
                "penalties_total": penalties_total,
                "net": net,
                "deals_count": deals_count or 0,
                "total_deals": total_deals or 0,
            })

        total_commission = sum(d["total_commission"] for d in comm_data)
        total_bonus = sum(d["total_bonus"] for d in comm_data)
        total_penalties = sum(d["penalties_total"] for d in comm_data)
        penalties_list = await penalty_repo.list_for_period(period)

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(
        user, comm_data=comm_data, all_comms=all_comms, user_map=user_map,
        current_period=period, total_commission=total_commission, total_bonus=total_bonus,
        total_penalties=total_penalties, penalties_list=penalties_list, csrf_token=csrf,
    )
    return templates.TemplateResponse(request, "team_commissions.html", ctx)


@app.get("/team/commissions/export/csv")
async def team_commissions_export_csv(request: Request, period: str = Query(None)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    if not period:
        period = utcnow().strftime("%Y-%m")

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()
        user_map = {u.id: u for u in active_users}

        comm_repo = CommissionRepository(session, tenant_id)
        summary = await comm_repo.get_summary(period)

        penalty_repo = PenaltyRepository(session, tenant_id)

        rows = []
        for row in summary:
            uid, total_comm, total_bonus, total_deals, deals_count = row
            penalties_total = await penalty_repo.get_total(uid)
            net = (total_comm or 0) + (total_bonus or 0) - penalties_total
            u = user_map.get(uid)
            rows.append({
                "user": u.full_name or u.username if u else "?",
                "deals": deals_count or 0,
                "commission": f"{(total_comm or 0):.2f}",
                "bonus": f"{(total_bonus or 0):.2f}",
                "penalties": f"{penalties_total:.2f}",
                "net": f"{net:.2f}",
            })

    import csv, io
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["user", "deals", "commission", "bonus", "penalties", "net"])
    writer.writeheader()
    writer.writerows(rows)

    from starlette.responses import StreamingResponse
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=commissions_{period}.csv"},
    )


@app.post("/team/commissions/add-penalty")
async def team_commissions_add_penalty(
    request: Request,
    user_id: int = Form(...),
    reason: str = Form(...),
    amount: float = Form(...),
    description: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = PenaltyRepository(session, tenant_id)
        await repo.create(user_id, reason, amount, description)
        await session.commit()

    return RedirectResponse("/team/commissions?success=Штраф+начислен", status_code=303)


@app.get("/team/analytics", response_class=HTMLResponse)
async def team_analytics_page(request: Request, days: int = Query(30)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()

        response_repo = ResponseTimeRepository(session, tenant_id)
        leads_repo = LeadRepository(session, tenant_id)

        analytics_data = []
        for u in active_users:
            avg_response = await response_repo.get_avg_response_time(u.id, days)
            total_leads = await leads_repo.count(Lead.assigned_to == u.id, Lead.status != "deleted")
            deals = await leads_repo.count(Lead.assigned_to == u.id, Lead.status == "deal")
            conversion = round(deals / total_leads * 100) if total_leads > 0 else 0

            # Source breakdown
            source_rows = (await session.execute(
                select(Lead.chat_title, func.count(Lead.id)).where(
                    Lead.assigned_to == u.id, Lead.status != "deleted"
                ).group_by(Lead.chat_title).order_by(func.count(Lead.id).desc()).limit(5)
            )).fetchall()

            analytics_data.append({
                "user": u,
                "avg_response_seconds": avg_response,
                "avg_response_label": _format_duration(avg_response),
                "total_leads": total_leads,
                "deals": deals,
                "conversion": conversion,
                "top_sources": [{"name": s[:30], "count": c} for s, c in source_rows],
            })

    ctx = await _template_ctx(user, analytics_data=analytics_data, days=days)
    return templates.TemplateResponse(request, "team_analytics.html", ctx)


@app.get("/team/timesheet", response_class=HTMLResponse)
async def team_timesheet_page(request: Request, date: str = Query(None)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    if not date:
        date = utcnow().strftime("%Y-%m-%d")

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()

        ws_repo = WorkSessionRepository(session, tenant_id)
        sessions = await ws_repo.list_all(date)

        ws_data = []
        for s in sessions:
            u = next((u for u in active_users if u.id == s.user_id), None)
            hours = round(s.total_seconds / 3600, 1) if s.total_seconds else 0
            ws_data.append({
                "session": s,
                "user": u,
                "hours": hours,
            })

    ctx = await _template_ctx(user, ws_data=ws_data, current_date=date)
    return templates.TemplateResponse(request, "team_timesheet.html", ctx)


@app.get("/team/timesheet/week")
async def team_timesheet_week(request: Request, date: str = Query(None)):
    """Weekly timesheet: per-user daily hours for 7 days starting from `date`."""
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)

    base = utcnow() - timedelta(days=utcnow().weekday())
    if date:
        try:
            base = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            pass
    base = base.replace(hour=0, minute=0, second=0, microsecond=0)
    days = [base + timedelta(days=i) for i in range(7)]

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()
        from app.models import WorkSession
        week_rows = (await session.execute(
            select(WorkSession).where(
                WorkSession.started_at >= days[0],
                WorkSession.started_at < days[-1] + timedelta(days=1),
            )
        )).scalars().all()

    grid = {u.id: {"user": u, "hours": [0.0] * 7, "total": 0.0} for u in active_users}
    for s in week_rows:
        if s.user_id not in grid or not s.total_seconds:
            continue
        for i, d in enumerate(days):
            if s.started_at.date() == d.date():
                h = round(s.total_seconds / 3600, 1)
                grid[s.user_id]["hours"][i] += h
                grid[s.user_id]["total"] += h
                break

    week_data = sorted(grid.values(), key=lambda g: -g["total"])
    prev_week = (days[0] - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (days[-1] + timedelta(days=1)).strftime("%Y-%m-%d")
    ctx = await _template_ctx(user, week_data=week_data, week_days=days,
                              prev_week=prev_week, next_week=next_week,
                              week_start=days[0].strftime("%Y-%m-%d"))
    return templates.TemplateResponse(request, "team_timesheet_week.html", ctx)


@app.get("/team/export/csv")
async def team_export_csv(request: Request):
    """CSV: per-manager performance snapshot."""
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        active_users = await users_repo.list_active()
        rows_out = []
        for u in active_users:
            if u.role not in ("admin", "manager"):
                continue
            total = (await session.execute(
                select(func.count(Lead.id)).where(
                    Lead.assigned_to == u.id, Lead.status != "deleted")
            )).scalar() or 0
            deals = (await session.execute(
                select(func.count(Lead.id)).where(
                    Lead.assigned_to == u.id, Lead.status == "deal")
            )).scalar() or 0
            revenue = (await session.execute(
                select(func.sum(Lead.deal_amount)).where(
                    Lead.assigned_to == u.id, Lead.status == "deal")
            )).scalar() or 0
            conv = round(deals / total * 100, 1) if total else 0
            rows_out.append([u.full_name or u.username, u.role, total, deals, conv, float(revenue or 0)])

    import csv as _csv
    from io import StringIO
    buf = StringIO()
    w = _csv.writer(buf)
    w.writerow(["Сотрудник", "Роль", "Лидов", "Сделок", "Конверсия %", "Выручка"])
    for r in rows_out:
        w.writerow(r)
    buf.seek(0)
    from fastapi.responses import Response as _Resp
    return _Resp(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                 headers={"Content-Disposition": "attachment; filename=team.csv"})


def _format_duration(seconds: float) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}с"
    if s < 3600:
        return f"{s // 60}м {s % 60}с"
    return f"{s // 3600}ч {(s % 3600) // 60}м"


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
    since = utcnow() - timedelta(days=days)

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
    since = utcnow() - timedelta(days=days)

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
    pdf.cell(0, 6, f"Generated: {utcnow().strftime('%d.%m.%Y %H:%M')} UTC", ln=True)
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

    filename = f"activity_{days}d_{utcnow().strftime('%Y%m%d_%H%M')}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ==================== MANAGER AUDIT PAGE ====================

@app.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    user_id: int = Query(None),
    evidence_type: str = Query(None),
):
    """Manager audit page - suspicious activity detection."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        all_users = await users_repo.list_active()
        user_map = {u.id: u for u in all_users}

        theft_repo = TheftDetectionRepository(session, tenant_id)
        
        # Run all detectors
        fake_rejects = await theft_repo.detect_fake_rejects(days=days)
        silent_takes = await theft_repo.detect_silent_takes(hours=24)
        quick_deals = await theft_repo.detect_quick_deals(min_hours=1.0)
        
        # Combine all evidence
        all_evidence = fake_rejects + silent_takes + quick_deals
        all_evidence.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        
        # Filter by user if specified
        if user_id:
            all_evidence = [e for e in all_evidence if e.get("suspect_user_id") == user_id]
        
        # Filter by type if specified
        if evidence_type:
            all_evidence = [e for e in all_evidence if e.get("evidence_type") == evidence_type]
        
        # Get existing confirmed evidence
        confirmed = await theft_repo.get_suspicious_activity(confirmed_only=True)

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(
        current_user,
        all_users=all_users, user_map=user_map,
        evidence=all_evidence, confirmed=confirmed,
        days=days, selected_user_id=user_id, selected_type=evidence_type,
        csrf_token=csrf,
    )
    return templates.TemplateResponse(request, "audit.html", ctx)


@app.post("/audit/confirm")
async def confirm_evidence(
    request: Request,
    evidence_type: str = Form(...),
    lead_id: int = Form(...),
    suspect_user_id: int = Form(...),
    confidence: float = Form(...),
    csrf_token: str = Form(...),
):
    """Confirm suspicious evidence and create penalty."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    if not validate_csrf_token(_get_session_id(request), csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        # Get contract for penalty amount
        contract_repo = ManagerContractRepository(session, tenant_id)
        contract = await contract_repo.get_active_contract(suspect_user_id)
        
        penalty_amount = 0
        if contract:
            if evidence_type == "fake_reject":
                penalty_amount = float(contract.penalty_per_fake_reject)
            elif evidence_type == "stolen_lead":
                penalty_amount = float(contract.penalty_per_stolen_lead)
            elif evidence_type == "sla_breach":
                penalty_amount = float(contract.penalty_per_sla_breach)
        
        # Create penalty
        penalty_repo = PenaltyRepository(session, tenant_id)
        penalty = await penalty_repo.create(
            user_id=suspect_user_id,
            reason="stolen_lead" if evidence_type in ("fake_reject", "stolen_lead") else "slow_response",
            amount=penalty_amount,
            description=f"Auto-detected: {evidence_type} (confidence: {confidence}%)",
            lead_id=lead_id,
        )
        
        # Create evidence record
        theft_repo = TheftDetectionRepository(session, tenant_id)
        evidence = await theft_repo.create_evidence(
            lead_id=lead_id,
            suspect_user_id=suspect_user_id,
            evidence_type=evidence_type,
            confidence=confidence,
        )
        await theft_repo.confirm_evidence(evidence.id, current_user.id, penalty.id)
        
        await session.commit()

    return RedirectResponse("/audit", status_code=303)


@app.get("/audit/api/evidence")
async def api_evidence(
    request: Request,
    days: int = Query(30, ge=1, le=90),
):
    """API endpoint for audit evidence (JSON)."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        theft_repo = TheftDetectionRepository(session, tenant_id)
        fake_rejects = await theft_repo.detect_fake_rejects(days=days)
        silent_takes = await theft_repo.detect_silent_takes(hours=24)
        quick_deals = await theft_repo.detect_quick_deals(min_hours=1.0)
        
        all_evidence = fake_rejects + silent_takes + quick_deals
        all_evidence.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    return {"evidence": all_evidence, "total": len(all_evidence)}


# ==================== MANAGER CONTRACTS ====================

@app.get("/contracts", response_class=HTMLResponse)
async def contracts_page(request: Request):
    """Manager contracts list page."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        all_users = await users_repo.list_active()
        user_map = {u.id: u for u in all_users}

        contract_repo = ManagerContractRepository(session, tenant_id)
        contracts = await contract_repo.list_contracts(include_expired=True)

    ctx = await _template_ctx(
        current_user,
        all_users=all_users, user_map=user_map,
        contracts=contracts,
    )
    return templates.TemplateResponse(request, "contracts.html", ctx)


@app.post("/contracts/create")
async def create_contract(
    request: Request,
    user_id: int = Form(...),
    commission_rate: float = Form(5.0),
    min_deal_amount: float = Form(10000.0),
    penalty_per_stolen_lead: float = Form(10000.0),
    penalty_per_sla_breach: float = Form(1000.0),
    penalty_per_fake_reject: float = Form(5000.0),
    sla_hours: int = Form(24),
    csrf_token: str = Form(...),
):
    """Create a new manager contract."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    if not validate_csrf_token(_get_session_id(request), csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        contract_repo = ManagerContractRepository(session, tenant_id)
        
        # Generate contract number
        import random
        contract_number = f"ДОГ-{random.randint(1000, 9999)}-{utcnow().strftime('%m%Y')}"
        
        contract = await contract_repo.create(
            user_id=user_id,
            contract_number=contract_number,
            commission_rate=commission_rate,
            min_deal_amount=min_deal_amount,
            penalty_per_stolen_lead=penalty_per_stolen_lead,
            penalty_per_sla_breach=penalty_per_sla_breach,
            penalty_per_fake_reject=penalty_per_fake_reject,
            sla_hours=sla_hours,
        )
        await session.commit()

    return RedirectResponse("/contracts", status_code=303)


@app.get("/contracts/{contract_id}", response_class=HTMLResponse)
async def contract_detail(request: Request, contract_id: int):
    """Contract detail page (HTML view)."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        contract_repo = ManagerContractRepository(session, tenant_id)
        contract = await session.get(ManagerContract, contract_id)
        if not contract:
            raise HTTPException(status_code=404)
        
        user = await session.get(User, contract.user_id)
        tenant = await session.get(Tenant, tenant_id)

    ctx = {
        "contract": contract,
        "user": user,
        "tenant": tenant,
        "tenant_director_name": tenant.config.get("director_name", "") if tenant.config else "",
    }
    return templates.TemplateResponse(request, "contract.html", ctx)


@app.get("/contracts/{contract_id}/sign")
async def sign_contract(request: Request, contract_id: int):
    """Mark contract as signed."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        contract_repo = ManagerContractRepository(session, tenant_id)
        await contract_repo.sign_contract(contract_id)
        await session.commit()

    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


# ==================== PAYMENTS PAGE ====================

@app.get("/payments", response_class=HTMLResponse)
async def payments_page(
    request: Request,
    period: str = Query(None),
    user_id: int = Query(None),
    status_filter: str = Query(None),
):
    """Payments page - commission tracking."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    tenant_id = await _get_tenant_id(current_user)
    
    # Default to current month if no period specified
    if not period:
        period = utcnow().strftime("%Y-%m")

    async with async_session() as session:
        users_repo = UserRepository(session, tenant_id)
        all_users = await users_repo.list_active()
        user_map = {u.id: u for u in all_users}

        commission_repo = CommissionRepository(session, tenant_id)
        penalty_repo = PenaltyRepository(session, tenant_id)
        
        # Get commissions for period
        commissions = await commission_repo.list_all(period=period)
        
        # Filter by user if specified
        if user_id:
            commissions = [c for c in commissions if c.user_id == user_id]
        
        # Filter by status if specified
        if status_filter:
            commissions = [c for c in commissions if c.status == status_filter]
        
        # Get summary
        summary = await commission_repo.get_summary(period)
        summary_map = {s.user_id: s for s in summary}
        
        # Get penalties for period
        all_penalties = []
        for u in all_users:
            user_penalties = await penalty_repo.list_by_user(u.id)
            # Filter by period (created in same month)
            period_penalties = [p for p in user_penalties if p.created_at and p.created_at.strftime("%Y-%m") == period]
            all_penalties.extend(period_penalties)

    ctx = await _template_ctx(
        current_user,
        all_users=all_users, user_map=user_map,
        commissions=commissions, summary=summary_map,
        penalties=all_penalties,
        period=period, selected_user_id=user_id, selected_status=status_filter,
    )
    return templates.TemplateResponse(request, "payments.html", ctx)


@app.post("/payments/{commission_id}/approve")
async def approve_payment(
    request: Request,
    commission_id: int,
    csrf_token: str = Form(...),
):
    """Approve that client has paid."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    if not validate_csrf_token(_get_session_id(request), csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        commission = await session.get(Commission, commission_id)
        if commission:
            commission.status = "approved"
            commission.approved_by = current_user.id
            commission.approved_at = utcnow()
            await session.commit()

    return RedirectResponse("/payments", status_code=303)


@app.post("/payments/{commission_id}/pay")
async def mark_paid(
    request: Request,
    commission_id: int,
    csrf_token: str = Form(...),
):
    """Mark commission as paid to manager."""
    current_user = await get_current_user(request)
    if not current_user or current_user.role != "super_admin":
        raise HTTPException(status_code=403)

    if not validate_csrf_token(_get_session_id(request), csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    tenant_id = await _get_tenant_id(current_user)

    async with async_session() as session:
        commission_repo = CommissionRepository(session, tenant_id)
        await commission_repo.mark_paid(commission_id)
        
        # Update status too
        commission = await session.get(Commission, commission_id)
        if commission:
            commission.status = "paid"
            commission.paid_at = utcnow()
        
        await session.commit()

    return RedirectResponse("/payments", status_code=303)


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
        new_tenant = await tenant_repo.create(name=name, slug=slug, city=city or None)
        await session.commit()
        tenant_id = new_tenant.id

        # Add default message templates for new tenant
        default_templates = [
            ("Первое обращение", "first_contact", "Здравствуйте, {name}! 👋\nВидели ваше сообщение в чате. Мы — команда по ремонту квартир в {city}.\nДелаем полный ремонт под ключ: от черновой отделки до чистовой.\nМожем бесплатно выехать на замер и рассчитать стоимость.\nКогда удобно посмотреть квартиру?"),
            ("Повторное обращение", "follow_up", "Добрый день, {name}!\nУточняли по поводу ремонта — готовы обсудить детали.\nКакой формат вам удобен: выезд на замер или предварительный расчёт по фото?"),
            ("Закрытие сделки", "deal_close", "{name}, рады, что вы выбрали нас! 🎉\nДоговорились: выезд на замер {date} в {time}.\nНаш менеджер свяжется с вами для подтверждения.\nЕсли есть вопросы — пишите!"),
            ("Ответ на вопрос", "general", "Добрый день, {name}!\nСпасибо за интерес к нашему ремонту.\n{answer}\nЕсть ещё вопросы? С удовольствием отвечу!"),
            ("Благодарность за отзыв", "general", "{name}, большое спасибо за отзыв! 🙏\nРады, что вам понравился результат.\nЕсли знакомые ищут ремонт — будем благодарны за рекомендацию!"),
        ]
        tmpl_repo = MessageTemplateRepository(session, tenant_id)
        for t_name, t_category, t_body in default_templates:
            await tmpl_repo.create(name=t_name, category=t_category, body=t_body, tenant_id=tenant_id)
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


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    if not tenant_id:
        return RedirectResponse("/?error=Super admin must select a tenant", status_code=303)

    tc = await TenantConfig.create(tenant_id)

    async with async_session() as session:
        from app.models import User as UserModel
        from sqlalchemy import func as sa_func
        
        user_count = (await session.execute(
            select(sa_func.count(UserModel.id)).where(UserModel.tenant_id == tenant_id, UserModel.is_active == True)
        )).scalar() or 0
        
        chats = _load_chats()
        chat_count = len(chats)
        
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        leads_this_month = (await session.execute(
            select(sa_func.count(Lead.id)).where(
                Lead.tenant_id == tenant_id,
                Lead.created_at >= month_start,
                Lead.status != "deleted"
            )
        )).scalar() or 0
    
    current_plan = tc.plan_features
    current_plan_key = tc.plan
    plans = tc.all_plans
    
    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, 
        current_plan=current_plan, 
        current_plan_key=current_plan_key,
        plans=plans,
        user_count=user_count,
        chat_count=chat_count,
        leads_this_month=leads_this_month,
        csrf_token=csrf,
    )
    return templates.TemplateResponse(request, "billing.html", ctx)


@app.post("/billing/subscribe")
async def billing_subscribe(request: Request, plan: str = Form(...), csrf_token: str = Form("")):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    
    valid_plans = ["free", "pro", "enterprise"]
    if plan not in valid_plans:
        return RedirectResponse("/billing?error=Неверный+план", status_code=303)

    async with async_session() as session:
        from app.models import Tenant
        tenant = await session.get(Tenant, tenant_id)
        if tenant:
            tenant.plan = plan
            await session.commit()

    return RedirectResponse("/billing?success=Тариф+изменён", status_code=303)


@app.post("/billing/trial")
async def billing_trial(request: Request, csrf_token: str = Form("")):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    
    async with async_session() as session:
        from app.models import Tenant
        from datetime import timedelta
        tenant = await session.get(Tenant, tenant_id)
        if tenant and tenant.plan == "free":
            tenant.plan = "pro"
            tenant.trial_ends_at = utcnow() + timedelta(days=14)
            await session.commit()

    return RedirectResponse("/billing?success=Пробный+период+активирован", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, error: str = Query(None), success: str = Query(None)):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    chats = _load_chats()

    # Lead counts per chat (tenant-scoped)
    chat_lead_counts = {}
    async with async_session() as session:
        from sqlalchemy import func as sa_func
        q_filters = [Lead.status != "deleted"]
        if tenant_id is not None:
            q_filters.append(Lead.tenant_id == tenant_id)
        q = (
            select(Lead.chat_title, sa_func.count(Lead.id))
            .where(*q_filters)
            .group_by(Lead.chat_title)
        )
        rows = (await session.execute(q)).fetchall()
        chat_lead_counts = {r[0]: r[1] for r in rows}

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
    ctx = await _template_ctx(user, chats=chats, chat_lead_counts=chat_lead_counts, tenant_config=tenant_config, csrf_token=csrf, error=error, success=success)
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
    followup_hours: int = Form(24),
    followup_stage2_days: int = Form(3),
    followup_enabled: str = Form("false"),
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
        config["followup_hours"] = max(4, min(168, followup_hours))
        config["followup_stage2_days"] = max(0, min(30, followup_stage2_days))
        config["followup_enabled"] = followup_enabled == "true"
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
        if url_val and not re.fullmatch(r"https?://[^\s<>\"']+", url_val) and not url_val.startswith("/static/uploads/"):
            return RedirectResponse(f"/settings?error={url_name}: допустимы только HTTP/HTTPS URL или загруженный файл", status_code=303)

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


UPLOAD_DIR = Path(__file__).parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/x-icon", "image/vnd.microsoft.icon"}  # no SVG — stored XSS risk
MAX_UPLOAD_SIZE = 2 * 1024 * 1024  # 2MB


@app.post("/api/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form("logo"),
):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        return JSONResponse({"error": "Допустимые форматы: PNG, JPG, GIF, SVG, ICO"}, status_code=400)

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_SIZE:
        return JSONResponse({"error": "Максимальный размер 2 МБ"}, status_code=400)

    ext = file.filename.rsplit(".", 1)[-1] if "." in (file.filename or "") else "png"
    filename = f"{purpose}_{int(time.time())}.{ext}"
    filepath = UPLOAD_DIR / filename

    with open(filepath, "wb") as f:
        f.write(contents)

    url = f"/static/uploads/{filename}"
    return JSONResponse({"url": url, "filename": filename})


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


@app.get("/settings/prices/export")
async def prices_export(request: Request):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        repo = TenantRepository(session)
        tenant = await (repo.get_by_id(tenant_id) if tenant_id is not None
                        else repo.get_by_slug("default"))
        price_list = (tenant.config or {}).get("price_list", {}) if tenant else {}
    from fastapi.responses import Response as _Resp
    import json as _json
    return _Resp(content=_json.dumps(price_list, ensure_ascii=False, indent=2).encode("utf-8"),
                 media_type="application/json",
                 headers={"Content-Disposition": "attachment; filename=price_list.json"})


@app.post("/settings/prices/import")
async def prices_import(request: Request, data: str = Form(""), csrf_token: str = Form("")):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)
    tenant_id = await _get_tenant_id(user)
    try:
        parsed = json.loads(data)
        assert isinstance(parsed, dict)
    except Exception:
        return RedirectResponse("/settings?error=Импорт прайса: невалидный JSON", status_code=303)
    clean = {}
    for cat, v in parsed.items():
        if isinstance(v, dict) and ("min" in v or "max" in v):
            clean[cat] = {"min": int(v.get("min") or 0), "max": int(v.get("max") or 0),
                          "unit": str(v.get("unit", "₽/м²"))[:20]}
    async with async_session() as session:
        repo = TenantRepository(session)
        tenant = await (repo.get_by_id(tenant_id) if tenant_id is not None
                        else repo.get_by_slug("default"))
        if not tenant:
            return RedirectResponse("/settings?error=Тенант не найден", status_code=303)
        cfg = dict(tenant.config or {})
        cfg["price_list"] = clean
        tenant.config = cfg
        flag_modified(tenant, "config")
        await session.commit()
    return RedirectResponse(f"/settings?success=Прайс+импортирован+({len(clean)}+категорий)", status_code=303)


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
        existing_norm = existing["url"].lower().replace("https://", "").replace("http://", "").replace("t.me/", "").rstrip("/")
        if normalized == existing_norm or normalized in existing_norm or existing_norm in normalized:
            is_duplicate = True
            break

    if is_duplicate:
        return RedirectResponse("/settings?error=Такой чат уже добавлен", status_code=303)

    if len(chats) >= 100:
        return RedirectResponse("/settings?error=Максимум 100 чатов", status_code=303)

    chats.append({"url": chat, "active": True})
    _save_chats(chats)
    await _sync_tenant_chats(tenant_id)
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
    chats = [c for c in chats if c["url"] != chat]
    _save_chats(chats)
    await _sync_tenant_chats(tenant_id)
    return RedirectResponse("/settings?success=Чат+удалён", status_code=303)


@app.post("/settings/chats/toggle")
async def toggle_chat(
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
    for c in chats:
        if c["url"] == chat:
            c["active"] = not c.get("active", True)
            break
    _save_chats(chats)
    await _sync_tenant_chats(tenant_id)
    status = "включён" if any(c["url"] == chat and c.get("active", True) for c in chats) else "выключен"
    return RedirectResponse(f"/settings?success=Чат+{status}", status_code=303)


@app.get("/ml-status", response_class=HTMLResponse)
async def ml_status_page(request: Request):
    """ML model health: accuracy, training count, weights drift."""
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/", status_code=303)

    from app.ml_scorer import get_model_metrics, WEIGHTS_FILE
    metrics = await get_model_metrics()

    weights_info = {}
    try:
        if WEIGHTS_FILE.exists():
            data = json.loads(WEIGHTS_FILE.read_text())
            weights_info = {
                "training_count": data.get("training_count", 0),
                "updated_at": data.get("updated_at", ""),
                "top_weights": sorted(
                    ({"feature": k, "weight": round(v, 3)} for k, v in (data.get("weights") or {}).items()),
                    key=lambda x: abs(x["weight"]), reverse=True)[:8],
            }
    except Exception:
        pass

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, metrics=metrics, weights_info=weights_info, csrf_token=csrf)
    return templates.TemplateResponse(request, "ml_status.html", ctx)


@app.get("/api/stats")
async def api_stats(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        now = utcnow()
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


# ===== Billing Settings =====

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
    if user.role not in ("super_admin", "admin"):
        return RedirectResponse("/templates", status_code=303)
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
    if user.role not in ("super_admin", "admin"):
        return RedirectResponse("/templates", status_code=303)
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
    model = _settings.openai_model or "openai/gpt-oss-20b"
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
        "Верни ТОЛЬКО JSON без markdown без кодбеков без thinking:\n"
        '{"name": "название шаблона на русском (до 50 символов)", '
        '"body": "текст шаблона на русском (1-5 сообщений, с переносами строк)"}'
    )

    import aiohttp as _aiohttp
    for _attempt in range(5):
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
                    timeout=_aiohttp.ClientTimeout(total=45),
                ) as resp:
                    if resp.status == 429:
                        wait = 8
                        logging.warning("Groq rate limit on attempt %d, waiting %ds", _attempt + 1, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        err = await resp.text()
                        logging.error("Groq API error %s: %s", resp.status, err)
                        return HTMLResponse(content=f"Ошибка AI API: {resp.status}", status_code=502)
                    result = await resp.json()
                    text = result["choices"][0]["message"]["content"].strip()
                    # Strip thinking tags
                    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
                    text = re.sub(r'<reasoning>.*?</reasoning>', '', text, flags=re.DOTALL)
                    if text.startswith("```"):
                        text = text.split("\n", 1)[1]
                    if text.endswith("```"):
                        text = text.rsplit("```", 1)[0]
                    text = text.strip()
                    json_match = re.search(r'\{[^{}]*"name"[^{}]*"body"[^{}]*\}', text, re.DOTALL)
                    if json_match:
                        text = json_match.group(0)
                    parsed = json.loads(text, strict=False)
                    return HTMLResponse(
                        content=json.dumps({"name": parsed.get("name", ""), "body": parsed.get("body", "")}),
                        media_type="application/json",
                    )
        except Exception as e:
            logging.warning("Template generation error attempt %d: %s", _attempt + 1, e)
            await asyncio.sleep(5)
            continue
    return HTMLResponse(content="AI API временно недоступен из-за нагрузки. Подождите 30 сек и попробуйте снова", status_code=503)


@app.get("/kanban", response_class=HTMLResponse)
async def kanban_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tenant_id = await _get_tenant_id(user)
    manager_filter = Lead.assigned_to == user.id if user.role == "manager" else None
    async with async_session() as session:
        leads_repo = LeadRepository(session, tenant_id)
        all_leads = await leads_repo.list_all()
        # Prefetch assignees once — avoids N+1 lazy loads per card
        assignee_ids = {l.assigned_to for l in all_leads if l.assigned_to}
        assignee_map = {}
        if assignee_ids:
            from app.models import User as _U
            u_rows = (await session.execute(
                select(_U).where(_U.id.in_(assignee_ids))
            )).scalars().all()
            assignee_map = {u.id: u for u in u_rows}
        for l in all_leads:
            l.assigned_user = assignee_map.get(l.assigned_to)
        active_leads = [l for l in all_leads if l.status not in ("deleted", "archive")]
        if manager_filter is not None:
            active_leads = [l for l in active_leads if l.assigned_to == user.id]
        chats = sorted(set(l.chat_title for l in active_leads if l.chat_title))
    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, leads=active_leads, chats=chats, csrf_token=csrf)
    return templates.TemplateResponse(request, "kanban.html", ctx)


@app.post("/api/kanban/move")
async def kanban_move(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    lead_id = body.get("lead_id")
    new_status = body.get("status")
    reason = body.get("reason", "")
    if not lead_id or not new_status:
        raise HTTPException(status_code=400)
    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        leads = LeadRepository(session, tenant_id)
        lead = await leads.get_by_id(lead_id)
        if not lead:
            raise HTTPException(status_code=404)
        if user.role == "manager" and lead.assigned_to != user.id:
            raise HTTPException(status_code=403)
        old_status = lead.status
        lead.status = new_status
        if new_status == "not_interested" and reason:
            lead.feedback_reason = reason[:50]
        history = LeadHistoryRepository(session, tenant_id)
        await history.create(
            lead_id=lead_id, user_id=user.id, action="status_change",
            old_value=old_status, new_value=new_status,
            note=f"{user.full_name or user.username} (канбан)" + (f": {reason}" if reason else ""),
        )
        await ActionLogRepository(session, tenant_id).log(
            user.id, "status_change", lead_id=lead_id,
            meta={"from": old_status, "to": new_status, "via": "kanban", "reason": reason},
        )
        await session.commit()

        try:
            from app.ml_scorer import train_on_outcome
            if new_status == "deal":
                lead.feedback = "useful"
                train_on_outcome(lead, is_positive=True)
            elif new_status in ("not_interested", "deleted"):
                lead.feedback = "not_useful"
                train_on_outcome(lead, is_positive=False)
        except Exception:
            pass

    await _fire_webhooks(tenant_id, "status_change", {"lead_id": lead_id, "from": old_status, "to": new_status})
    return {"ok": True}


@app.post("/api/leads/{lead_id}/not-lead")
async def api_lead_not_lead(request: Request, lead_id: int):
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

        old_status = lead.status
        lead.feedback = "not_useful"
        lead.feedback_reason = "off_topic"
        lead.status = "deleted"
        await LeadHistoryRepository(session, tenant_id).create(
            lead_id=lead_id, user_id=user.id, action="status_change",
            old_value=old_status, new_value="deleted",
            note=f"{user.full_name or user.username}: отмечено «не лид» (канбан)",
        )
        await ActionLogRepository(session, tenant_id).log(
            user.id, "mark_not_lead", lead_id=lead_id,
            meta={"from": old_status, "via": "kanban"},
        )
        await session.commit()

        try:
            from app.ml_scorer import train_on_outcome
            train_on_outcome(lead, is_positive=False, learning_rate=0.03)
        except Exception:
            pass

    return {"ok": True}


@app.get("/funnel", response_class=HTMLResponse)
async def funnel_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role not in ("super_admin", "admin"):
        return RedirectResponse("/", status_code=303)
    tenant_id = await _get_tenant_id(user)

    days = int(request.query_params.get("days", 30))
    if days not in (1, 7, 30, 90):
        days = 30
    since = utcnow() - timedelta(days=days)

    from app.models import ActionLog

    async with async_session() as session:
        lead_filters = [Lead.created_at >= since]
        log_filters = [ActionLog.created_at >= since]
        if tenant_id is not None:
            lead_filters.append(Lead.tenant_id == tenant_id)
            log_filters.append(ActionLog.tenant_id == tenant_id)

        status_rows = (await session.execute(
            select(Lead.status, func.count(Lead.id)).where(*lead_filters).group_by(Lead.status)
        )).fetchall()
        status_counts = {s: c for s, c in status_rows}

        deal_row = (await session.execute(
            select(func.count(Lead.id), func.coalesce(func.sum(Lead.deal_amount), 0))
            .where(*lead_filters, Lead.status == "deal")
        )).first()
        deals_count = int(deal_row[0] or 0)
        deals_sum = float(deal_row[1] or 0)

        event_rows = (await session.execute(
            select(
                ActionLog.action_type,
                func.count(func.distinct(ActionLog.lead_id)),
                func.count(ActionLog.id),
            )
            .where(*log_filters, ActionLog.action_type.in_(
                ["lead_view", "click_write", "profile_click", "mark_not_lead"]))
            .group_by(ActionLog.action_type)
        )).fetchall()
        events = {a: {"leads": int(d or 0), "total": int(t or 0)} for a, d, t in event_rows}

        # === Quality dashboard: rejection stats ===
        from app.models import FilterStat
        fs_filters = [FilterStat.day >= since.date()]
        if tenant_id is not None:
            fs_filters.append(FilterStat.tenant_id == tenant_id)

        rej_rows = (await session.execute(
            select(FilterStat.source, func.sum(FilterStat.cnt))
            .where(*fs_filters).group_by(FilterStat.source)
        )).fetchall()
        reject_by_source = {s: int(c or 0) for s, c in rej_rows}

        reason_rows = (await session.execute(
            select(FilterStat.reason, FilterStat.source, func.sum(FilterStat.cnt))
            .where(*fs_filters).group_by(FilterStat.reason, FilterStat.source)
            .order_by(func.sum(FilterStat.cnt).desc()).limit(10)
        )).fetchall()
        top_reasons = [{"reason": r, "source": s, "cnt": int(c)} for r, s, c in reason_rows]

        blind_rows = (await session.execute(
            select(FilterStat.chat_title, func.sum(FilterStat.cnt))
            .where(*fs_filters).group_by(FilterStat.chat_title)
            .order_by(func.sum(FilterStat.cnt).desc()).limit(6)
        )).fetchall()
        blind_zones = [{"chat": c or "—", "cnt": int(n)} for c, n in blind_rows]

        # === Sources (chats) breakdown by deals ===
        chat_rows = (await session.execute(
            select(Lead.chat_title,
                   func.count(Lead.id),
                   func.sum(case((Lead.status == "deal", 1), else_=0)),
                   func.coalesce(func.sum(case((Lead.status == "deal", Lead.deal_amount), else_=None)), 0))
            .where(*lead_filters).group_by(Lead.chat_title)
        )).fetchall()
        sources = sorted(
            [{"title": t or "—", "leads": int(l), "deals": int(d or 0), "sum": float(sm or 0)}
             for t, l, d, sm in chat_rows],
            key=lambda x: x["sum"], reverse=True)[:15]

        # === Auto-suggestions from feedback analysis ===
        from app.analyzer import analyze_feedback
        try:
            fb = await analyze_feedback(tenant_id)
        except Exception:
            fb = {}

        # === Pipeline forecast: open leads weighted by status ===
        open_weights = {"new": 0.05, "contacted": 0.15, "in_progress": 0.35, "interested": 0.5}
        pipe_filters = [Lead.status.in_(list(open_weights.keys()))]
        if tenant_id is not None:
            pipe_filters.append(Lead.tenant_id == tenant_id)
        pipe_rows = (await session.execute(
            select(Lead.status, func.count(Lead.id))
            .where(*pipe_filters).group_by(Lead.status)
        )).fetchall()
        pipe_counts = {s: int(c or 0) for s, c in pipe_rows}

        avg_filters = [Lead.status == "deal", Lead.deal_amount.isnot(None)]
        if tenant_id is not None:
            avg_filters.append(Lead.tenant_id == tenant_id)
        avg_deal = (await session.execute(
            select(func.coalesce(func.avg(Lead.deal_amount), 0)).where(*avg_filters)
        )).scalar() or 0
        avg_deal = float(avg_deal)

    total_leads = sum(status_counts.values())
    not_lead_count = status_counts.get("deleted", 0) + events.get("mark_not_lead", {}).get("total", 0)

    status_meta = [
        ("new", "Новые"), ("contacted", "Контакт установлен"),
        ("interested", "Заинтересованы"), ("in_progress", "В работе"),
        ("deal", "Сделка"), ("not_interested", "Отказ клиента"),
        ("missed_call", "Пропущенный звонок"), ("archive", "Архив"),
        ("deleted", "Не лид / удалён"),
    ]
    stages = [{"key": k, "label": lbl, "count": status_counts.get(k, 0)} for k, lbl in status_meta]
    max_stage = max((s["count"] for s in stages), default=0) or 1

    open_weights = {"new": 0.05, "contacted": 0.15, "in_progress": 0.35, "interested": 0.5}
    pipe_labels = {"new": "Новые", "contacted": "Контакт", "in_progress": "В работе",
                   "interested": "Заинтересованы"}
    pipeline_stages = []
    for k, w in open_weights.items():
        cnt = pipe_counts.get(k, 0)
        pipeline_stages.append({
            "key": k, "label": pipe_labels[k], "count": cnt, "weight": int(w * 100),
            "value": cnt * w * avg_deal,
        })
    pipeline_total = sum(s["value"] for s in pipeline_stages)

    steps = [
        {"label": "Лиды создано", "value": total_leads},
        {"label": "Карточек просмотрено (уник. лидов)", "value": events.get("lead_view", {}).get("leads", 0)},
        {"label": "Открыт профиль Telegram", "value": events.get("profile_click", {}).get("leads", 0)},
        {"label": "Клик «Написать в Telegram»", "value": events.get("click_write", {}).get("leads", 0)},
        {"label": "Сделок закрыто", "value": deals_count},
    ]
    max_step = max((s["value"] for s in steps), default=0) or 1
    prev_value = None
    for s in steps:
        s["pct"] = round(s["value"] / max_step * 100)
        s["conv"] = round(s["value"] / prev_value * 100) if prev_value else None
        prev_value = s["value"] or prev_value

    ctx = await _template_ctx(user, stages=stages, steps=steps, max_stage=max_stage,
                              days=days, deals_sum=deals_sum, not_lead_count=not_lead_count,
                              reject_by_source=reject_by_source, top_reasons=top_reasons,
                              blind_zones=blind_zones, sources=sources,
                              suggestions=fb.get("suggestions", [])[:8],
                              fb_message=fb.get("message", ""),
                              fb_total=fb.get("total_feedback", 0),
                              pipeline_stages=pipeline_stages, pipeline_total=pipeline_total,
                              avg_deal=avg_deal)
    return templates.TemplateResponse(request, "funnel.html", ctx)


@app.post("/api/config/add-word")
async def api_add_config_word(request: Request):
    """Add a single word to tenant keywords/noise_keywords (from quality suggestions)."""
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403)
    body = await request.json()
    field = body.get("field")
    word = (body.get("word") or "").strip().lower()
    if field not in ("keywords", "noise_keywords") or not word or len(word) > 60:
        raise HTTPException(status_code=400)
    tenant_id = await _get_tenant_id(user)

    async with async_session() as session:
        repo = TenantRepository(session)
        tenant = await (repo.get_by_id(tenant_id) if tenant_id is not None
                        else repo.get_by_slug("default"))
        if not tenant:
            raise HTTPException(status_code=404)
        cfg = dict(tenant.config or {})
        words = [w for w in (cfg.get(field) or []) if isinstance(w, str)]
        if word not in words:
            words.append(word)
            cfg[field] = words[:500]
            tenant.config = cfg
            flag_modified(tenant, "config")
            await session.commit()
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
    return {"ok": True}


@app.get("/sources", response_class=HTMLResponse)
async def sources_list(request: Request):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        from app.models import LeadSource
        result = await session.execute(
            select(LeadSource).where(LeadSource.tenant_id == tenant_id).order_by(LeadSource.created_at.desc())
        )
        sources = result.scalars().all()

    from app.scrapers.cities import list_cities
    from datetime import datetime

    from app.models import ChatSuggestion
    async with async_session() as session:
        pending_suggestions = (await session.execute(
            select(ChatSuggestion)
            .where(ChatSuggestion.tenant_id == tenant_id, ChatSuggestion.status == "pending")
            .order_by(ChatSuggestion.members_count.desc())
            .limit(20)
        )).scalars().all()

    csrf = generate_csrf_token(_get_session_id(request))
    ctx = await _template_ctx(user, sources=sources, csrf_token=csrf, cities=list_cities(),
                              now=datetime.utcnow(), chat_suggestions=pending_suggestions)
    return templates.TemplateResponse(request, "sources.html", ctx)


@app.post("/api/chat-suggestions/{suggestion_id}/approve")
async def api_chat_suggestion_approve(request: Request, suggestion_id: int):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403)
    tenant_id = await _get_tenant_id(user)

    from app.models import ChatSuggestion
    async with async_session() as session:
        sug = (await session.execute(
            select(ChatSuggestion).where(
                ChatSuggestion.id == suggestion_id,
                ChatSuggestion.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if not sug:
            raise HTTPException(status_code=404)

        repo = TenantRepository(session)
        tenant = await (repo.get_by_id(tenant_id) if tenant_id is not None
                        else repo.get_by_slug("default"))
        if not tenant:
            raise HTTPException(status_code=404)

        url = f"https://t.me/{sug.username}"
        cfg = dict(tenant.config or {})
        chats = cfg.get("monitored_chats", "")
        if isinstance(chats, list):
            chat_list = [str(c) for c in chats]
        else:
            chat_list = [c.strip() for c in (chats or "").split(",") if c.strip()]
        if url not in chat_list:
            chat_list.append(url)
        cfg["monitored_chats"] = ",".join(chat_list[:200])
        tenant.config = cfg
        flag_modified(tenant, "config")

        sug.status = "approved"
        await session.commit()
    return {"ok": True, "url": url}


@app.post("/api/chat-suggestions/{suggestion_id}/reject")
async def api_chat_suggestion_reject(request: Request, suggestion_id: int):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403)
    tenant_id = await _get_tenant_id(user)

    from app.models import ChatSuggestion
    async with async_session() as session:
        sug = (await session.execute(
            select(ChatSuggestion).where(
                ChatSuggestion.id == suggestion_id,
                ChatSuggestion.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if not sug:
            raise HTTPException(status_code=404)
        sug.status = "rejected"
        await session.commit()
    return {"ok": True}


@app.post("/sources/add")
async def source_add(
    request: Request,
    name: str = Form(...),
    city: str = Form(""),
    vk_token: str = Form(""),
    proxy: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    from app.scrapers.cities import (
        resolve_city, get_avito_slug, get_cian_region_id,
        DEFAULT_QUERIES, DEFAULT_FORUMHOUSE_SECTIONS,
    )

    tenant_id = await _get_tenant_id(user)
    city = city.strip()
    display_names = {"vk": "VK", "avito": "Avito", "cian": "ЦИАН", "forumhouse": "ForumHouse"}

    config = {}

    if name == "vk":
        config = {
            "city": city or "Москва",
            "vk_access_token": vk_token,
            "queries": DEFAULT_QUERIES["vk"],
        }
    elif name == "avito":
        config = {
            "city": city or "Москва",
            "queries": DEFAULT_QUERIES["avito"],
        }
        if proxy.strip():
            config["proxy"] = proxy.strip()
    elif name == "cian":
        config = {
            "city": city or "Санкт-Петербург",
            "queries": DEFAULT_QUERIES["cian"],
        }
    elif name == "forumhouse":
        config = {
            "sections": DEFAULT_FORUMHOUSE_SECTIONS,
            "queries": DEFAULT_QUERIES["forumhouse"],
        }

    from app.models import LeadSource
    async with async_session() as session:
        source = LeadSource(
            tenant_id=tenant_id,
            name=name,
            display_name=display_names.get(name, name.upper()),
            config=config,
        )
        session.add(source)
        await session.commit()

    return RedirectResponse("/sources?success=Источник+добавлен", status_code=303)


@app.post("/sources/{source_id}/delete")
async def source_delete(request: Request, source_id: int, csrf_token: str = Form("")):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        from app.models import LeadSource
        result = await session.execute(
            select(LeadSource).where(LeadSource.id == source_id, LeadSource.tenant_id == tenant_id)
        )
        source = result.scalar_one_or_none()
        if source:
            source.is_active = False
            await session.commit()
    return RedirectResponse("/sources?success=Источник+удалён", status_code=303)


@app.post("/sources/{source_id}/toggle")
async def source_toggle(request: Request, source_id: int, csrf_token: str = Form("")):
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        from app.models import LeadSource
        result = await session.execute(
            select(LeadSource).where(LeadSource.id == source_id, LeadSource.tenant_id == tenant_id)
        )
        source = result.scalar_one_or_none()
        if source:
            source.is_active = not source.is_active
            await session.commit()
    return RedirectResponse("/sources?success=Статус+обновлён", status_code=303)


@app.post("/sources/{source_id}/test")
async def source_test(request: Request, source_id: int, csrf_token: str = Form("")):
    """Test a scraper — run it once and show results."""
    user = await get_current_user(request)
    if not user or user.role not in ("super_admin", "admin"):
        return RedirectResponse("/login", status_code=303)
    if not validate_csrf_token(csrf_token):
        return HTMLResponse(content="Invalid CSRF token", status_code=403)

    tenant_id = await _get_tenant_id(user)
    async with async_session() as session:
        from app.models import LeadSource
        result = await session.execute(
            select(LeadSource).where(LeadSource.id == source_id, LeadSource.tenant_id == tenant_id)
        )
        source = result.scalar_one_or_none()
        if not source:
            return RedirectResponse("/sources?error=Источник+не+найден", status_code=303)

    # Run the scraper
    try:
        from app.scrapers.avito_scraper import AvitoScraper
        from app.scrapers.cian_scraper import CIANScraper
        from app.scrapers.forumhouse_scraper import ForumHouseScraper
        from app.scrapers.vk_scraper import VKScraper

        config = source.config or {}
        scraper_map = {
            "vk": VKScraper,
            "avito": AvitoScraper,
            "cian": CIANScraper,
            "forumhouse": ForumHouseScraper,
        }
        scraper_cls = scraper_map.get(source.name)
        if not scraper_cls:
            return RedirectResponse("/sources?error=Неизвестный+тип+источника", status_code=303)

        scraper = scraper_cls(config)
        leads = await scraper.monitor(config.get("queries", []), cities=[config.get("city", "")])
        count = len(leads)
        sample_texts = [l.text[:100] for l in leads[:3]]
        errors = scraper.get_errors()
        error_html = ""
        if errors:
            error_items = ''.join(f'<div style="margin:4px 0;padding:6px;background:#3a1a1a;border-radius:4px;font-size:11px;color:#ff6b6b">{e["time"]}: {e["error"]}</div>' for e in errors)
            error_html = f'<p style="margin-top:12px;color:#ff6b6b"><strong>Ошибки:</strong></p>{error_items}'

        return HTMLResponse(content=f"""
        <html><body style="font-family:monospace;padding:20px;background:#1a1a2e;color:#e0e0e0">
        <h2>Тест {source.name}</h2>
        <p>Найдено лидов: <strong>{count}</strong></p>
        {''.join(f'<div style="margin:8px 0;padding:8px;background:#2a2a4a;border-radius:4px;font-size:12px">{t}...</div>' for t in sample_texts) if sample_texts else '<p style="color:#888">Лиды не найдены</p>'}
        {error_html}
        <p style="margin-top:16px"><a href="/sources" style="color:#c45a5a">← Назад</a></p>
        </body></html>
        """)
    except Exception as e:
        return HTMLResponse(content=f"""
        <html><body style="font-family:monospace;padding:20px;background:#1a1a2e;color:#e0e0e0">
        <h2>Ошибка теста {source.name}</h2>
        <pre style="color:#c45a5a">{str(e)}</pre>
        <p style="margin-top:16px"><a href="/sources" style="color:#c45a5a">← Назад</a></p>
        </body></html>
        """)


# ============================================================
# REST API для интеграций (1С, Битрикс, AmoCRM)
# ============================================================

from fastapi import Header, HTTPException
from typing import Optional


_prediction_cache = {"data": None, "ts": 0, "tenant": None}


def _verify_api_key(x_api_key: str = Header(None)) -> None:
    """Verify static API key from env (API_KEY). No cookie sessions for machine access."""
    if not settings.api_key:
        raise HTTPException(status_code=503, detail="API disabled: API_KEY not configured on server")
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


async def _api_tenant_id() -> Optional[int]:
    """Tenant scope for external API calls (API_TENANT_ID env or first active tenant)."""
    if settings.api_tenant_id:
        return settings.api_tenant_id
    async with async_session() as session:
        t = (await session.execute(
            select(Tenant).where(Tenant.is_active == True).order_by(Tenant.id).limit(1)
        )).scalar_one_or_none()
        return t.id if t else None


@app.get("/api/v1/leads")
async def api_leads_list(
    request: Request,
    status: Optional[str] = None,
    chat: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    x_api_key: str = Header(None),
):
    """REST API: Get leads list."""
    _verify_api_key(x_api_key)
    
    # Tenant scope for external API
    tenant_id = await _api_tenant_id()

    async with async_session() as session:
        leads_repo = LeadRepository(session, tenant_id)
        q = select(Lead).where(Lead.status != "deleted")
        count_q = select(func.count(Lead.id)).where(Lead.status != "deleted")

        if tenant_id:
            q = q.where(Lead.tenant_id == tenant_id)
            count_q = count_q.where(Lead.tenant_id == tenant_id)
        if status:
            q = q.where(Lead.status == status)
            count_q = count_q.where(Lead.status == status)
        if chat:
            q = q.where(Lead.chat_title.contains(chat))
            count_q = count_q.where(Lead.chat_title.contains(chat))

        total = (await session.execute(count_q)).scalar() or 0
        q = q.order_by(Lead.created_at.desc()).offset(offset).limit(limit)
        leads = (await session.execute(q)).scalars().all()
        
        return {
            "leads": [
                {
                    "id": l.id,
                    "first_name": l.first_name,
                    "last_name": l.last_name,
                    "username": l.username,
                    "phone": l.phone,
                    "message_text": l.message_text,
                    "chat_title": l.chat_title,
                    "lead_score": l.lead_score,
                    "urgency": l.urgency,
                    "status": l.status,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                }
                for l in leads
            ],
            "total": len(leads),
        }


@app.get("/api/v1/leads/{lead_id}")
async def api_leads_get(
    request: Request,
    lead_id: int,
    x_api_key: str = Header(None),
):
    """REST API: Get single lead."""
    _verify_api_key(x_api_key)
    tenant_id = await _api_tenant_id()
    
    async with async_session() as session:
        leads_repo = LeadRepository(session, tenant_id)
        lead = await leads_repo.get_by_id(lead_id)
        
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        
        return {
            "id": lead.id,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "username": lead.username,
            "phone": lead.phone,
            "user_id": lead.user_id,
            "message_text": lead.message_text,
            "chat_title": lead.chat_title,
            "lead_score": lead.lead_score,
            "urgency": lead.urgency,
            "status": lead.status,
            "reason": lead.reason,
            "recommended_message": lead.recommended_message,
            "created_at": lead.created_at.isoformat() if lead.created_at else None,
        }


@app.post("/api/v1/leads")
async def api_leads_create(
    request: Request,
    x_api_key: str = Header(None),
):
    """REST API: Create a lead (for CRM integrations)."""
    _verify_api_key(x_api_key)
    tenant_id = await _api_tenant_id()
    data = await request.json()
    
    async with async_session() as session:
        lead = Lead(
            tenant_id=tenant_id,
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            username=data.get("username"),
            user_id=data.get("user_id"),
            phone=data.get("phone"),
            message_text=data.get("message_text", ""),
            chat_title=data.get("chat_title", "API"),
            lead_score=max(0, min(100, int(data.get("lead_score", 50)))),
            urgency=data.get("urgency", "medium") if data.get("urgency") in ("low", "medium", "high") else "medium",
            status="new",
        )
        session.add(lead)
        await session.flush()
        session.add(LeadHistory(
            lead_id=lead.id, tenant_id=tenant_id, user_id=None,
            action="created", note="Создано через внешний API",
        ))
        await session.commit()
        await session.refresh(lead)

        await _fire_webhooks(tenant_id, "lead_created", {
            "lead_id": lead.id, "score": lead.lead_score, "chat": lead.chat_title,
        })

        return {"id": lead.id, "status": "created"}


@app.put("/api/v1/leads/{lead_id}")
async def api_leads_update(
    request: Request,
    lead_id: int,
    x_api_key: str = Header(None),
):
    """REST API: Update a lead status (for CRM integrations)."""
    _verify_api_key(x_api_key)
    tenant_id = await _api_tenant_id()
    data = await request.json()
    
    async with async_session() as session:
        leads_repo = LeadRepository(session, tenant_id)
        lead = await leads_repo.get_by_id(lead_id)

        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        valid_statuses = {"new", "contacted", "interested", "not_interested",
                          "deal", "archive", "deleted", "missed_call", "in_progress"}
        old_status = lead.status
        if "status" in data:
            if data["status"] not in valid_statuses:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid status. Allowed: {sorted(valid_statuses)}")
            lead.status = data["status"]
        if "phone" in data:
            lead.phone = data["phone"]
        if "lead_score" in data:
            lead.lead_score = max(0, min(100, float(data["lead_score"])))

        await session.commit()

        if "status" in data and lead.status != old_status:
            await _fire_webhooks(tenant_id, "status_change", {
                "lead_id": lead.id, "from": old_status, "to": lead.status,
            })

        return {"id": lead.id, "status": "updated"}


@app.get("/api/v1/stats")
async def api_stats(
    request: Request,
    x_api_key: str = Header(None),
):
    """REST API: Get statistics (for dashboards)."""
    _verify_api_key(x_api_key)
    tenant_id = await _api_tenant_id()
    
    async with async_session() as session:
        from sqlalchemy import func as sa_func
        
        total = (await session.execute(
            select(sa_func.count(Lead.id)).where(Lead.tenant_id == tenant_id, Lead.status != "deleted")
        )).scalar() or 0
        
        deals = (await session.execute(
            select(sa_func.count(Lead.id)).where(Lead.tenant_id == tenant_id, Lead.status == "deal")
        )).scalar() or 0
        
        revenue = (await session.execute(
            select(sa_func.sum(Lead.deal_amount)).where(
                Lead.tenant_id == tenant_id,
                Lead.deal_amount.isnot(None)
            )
        )).scalar() or 0
        
        return {
            "total_leads": total,
            "total_deals": deals,
            "total_revenue": float(revenue),
            "conversion_rate": round(deals / total * 100, 1) if total > 0 else 0,
        }
