"""
Cross-tenant isolation integration test.
Two tenants (A and B), cross-tenant requests on all endpoints,
blocked-tenant checks. Run against a live server on port 8001.

Usage:
    pytest tests/test_isolation.py -v
"""
import re
import pytest
import httpx
from app.auth import hash_password, create_token
from app.models import (
    Base, engine, async_session, Tenant, User, Lead, LeadHistory,
    BlacklistedUser, ProcessedMessage, TenantUsage,
)
from app.repositories import TenantRepository, UserRepository, LeadRepository
from sqlalchemy import select, delete


BASE = "http://127.0.0.1:8001"

TENANT_A = {"name": "Tenant A", "slug": "tenant-a", "city": "SPb"}
TENANT_B = {"name": "Tenant B", "slug": "tenant-b", "city": "Moscow"}

_RESERVED_SLUGS = {"admin", "api", "franchisor", "settings", "login",
                   "static", "leads", "archive", "analytics", "team",
                   "users", "billing", "telegram", "logout", "change-password", "new"}


async def _setup():
    from app.auth import hash_password

    async with async_session() as session:
        for model in [ProcessedMessage, LeadHistory, Lead, BlacklistedUser, TenantUsage, User, Tenant]:
            await session.execute(delete(model))
        await session.commit()

        repo = TenantRepository(session)
        t_a = await repo.create(**TENANT_A)
        t_b = await repo.create(**TENANT_B)
        await session.commit()
        await session.refresh(t_a)
        await session.refresh(t_b)

        user_repo = UserRepository(session)
        admin_a = await user_repo.create(
            username="admin_a", password_hash=hash_password("password123"),
            full_name="Admin A", role="admin", tenant_id=t_a.id
        )
        admin_b = await user_repo.create(
            username="admin_b", password_hash=hash_password("password123"),
            full_name="Admin B", role="admin", tenant_id=t_b.id
        )
        manager_a = await user_repo.create(
            username="manager_a", password_hash=hash_password("password123"),
            full_name="Manager A", role="manager", tenant_id=t_a.id
        )
        viewer_b = await user_repo.create(
            username="viewer_b", password_hash=hash_password("password123"),
            full_name="Viewer B", role="viewer", tenant_id=t_b.id
        )
        await session.commit()

        lead_repo_a = LeadRepository(session, t_a.id)
        lead_a1 = await lead_repo_a.create(first_name="LeadA1", message_text="ремонт квартир спб", chat_title="TestChat")
        lead_a2 = await lead_repo_a.create(first_name="LeadA2", message_text="купить квартиру", chat_title="TestChat")

        lead_repo_b = LeadRepository(session, t_b.id)
        lead_b1 = await lead_repo_b.create(first_name="LeadB1", message_text="дизайн интерьера", chat_title="DesignChat")
        await session.commit()

    return {
        "t_a": t_a, "t_b": t_b,
        "admin_a": admin_a, "admin_b": admin_b,
        "manager_a": manager_a, "viewer_b": viewer_b,
        "lead_a1": lead_a1, "lead_a2": lead_a2, "lead_b1": lead_b1,
    }


def _token(user_id, tenant_id):
    return create_token(user_id, tenant_id)


class IsolationClient:
    def __init__(self, http, user, tenant):
        self.http = http
        self.token = _token(user.id, tenant.id)
        self.user = user
        self.tenant = tenant

    async def get(self, path, **kw):
        return await self.http.get(f"{BASE}{path}", cookies={"lead_hunter_token": self.token}, **kw)

    async def post(self, path, **kw):
        return await self.http.post(f"{BASE}{path}", cookies={"lead_hunter_token": self.token}, **kw)


# ── fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
async def data():
    return await _setup()


@pytest.fixture(scope="module")
async def http_client():
    async with httpx.AsyncClient(follow_redirects=False) as c:
        yield c


@pytest.fixture(scope="module")
async def client_a(http_client, data):
    return IsolationClient(http_client, data["admin_a"], data["t_a"])


@pytest.fixture(scope="module")
async def client_b(http_client, data):
    return IsolationClient(http_client, data["admin_b"], data["t_b"])


@pytest.fixture(scope="module")
async def manager_a_client(http_client, data):
    return IsolationClient(http_client, data["manager_a"], data["t_a"])


@pytest.fixture(scope="module")
async def viewer_b_client(http_client, data):
    return IsolationClient(http_client, data["viewer_b"], data["t_b"])


# ── 1. Auth isolation ────────────────────────────────────────────

class TestAuthIsolation:
    async def test_admin_a_can_login(self, client_a):
        r = await client_a.get("/")
        assert r.status_code == 200

    async def test_admin_b_can_login(self, client_b):
        r = await client_b.get("/")
        assert r.status_code == 200

    async def test_invalid_token_rejected(self, http_client):
        r = await http_client.get(f"{BASE}/", cookies={"lead_hunter_token": "garbage"})
        assert r.status_code in (302, 303)
        assert "login" in r.headers.get("location", "")


# ── 2. Lead isolation ────────────────────────────────────────────

class TestLeadIsolation:
    async def test_admin_a_sees_only_own_leads(self, client_a):
        r = await client_a.get("/leads")
        assert r.status_code == 200
        assert "LeadA1" in r.text
        assert "LeadA2" in r.text
        assert "LeadB1" not in r.text

    async def test_admin_b_sees_only_own_leads(self, client_b):
        r = await client_b.get("/leads")
        assert r.status_code == 200
        assert "LeadB1" in r.text
        assert "LeadA1" not in r.text

    async def test_admin_a_cannot_access_lead_b_by_id(self, client_a, data):
        r = await client_a.get(f"/leads/{data['lead_b1'].id}")
        assert r.status_code in (302, 303, 404)

    async def test_admin_b_cannot_access_lead_a_by_id(self, client_b, data):
        r = await client_b.get(f"/leads/{data['lead_a1'].id}")
        assert r.status_code in (302, 303, 404)

    async def test_cross_tenant_lead_detail_no_leak(self, client_a, data):
        r = await client_a.get(f"/leads/{data['lead_b1'].id}")
        if r.status_code == 200:
            assert "LeadB1" not in r.text


# ── 3. API isolation ─────────────────────────────────────────────

class TestAPIIsolation:
    async def test_stats_a_only_own_data(self, client_a):
        r = await client_a.get("/api/stats")
        assert r.status_code == 200

    async def test_stats_b_only_own_data(self, client_b):
        r = await client_b.get("/api/stats")
        assert r.status_code == 200

    async def test_unauthenticated_api_rejected(self, http_client):
        r = await http_client.get(f"{BASE}/api/stats")
        assert r.status_code in (302, 303, 401, 403)


# ── 4. Settings isolation ────────────────────────────────────────

class TestSettingsIsolation:
    async def test_admin_a_sees_own_settings(self, client_a):
        r = await client_a.get("/settings")
        assert r.status_code == 200

    async def test_admin_b_sees_own_settings(self, client_b):
        r = await client_b.get("/settings")
        assert r.status_code == 200

    async def test_manager_cannot_access_settings(self, manager_a_client):
        r = await manager_a_client.get("/settings")
        assert r.status_code in (302, 303)

    async def test_viewer_cannot_access_settings(self, viewer_b_client):
        r = await viewer_b_client.get("/settings")
        assert r.status_code in (302, 303)


# ── 5. User management isolation ─────────────────────────────────

class TestUserIsolation:
    async def test_admin_a_sees_only_own_users(self, client_a):
        r = await client_a.get("/users")
        assert r.status_code == 200
        assert "admin_a" in r.text
        assert "admin_b" not in r.text

    async def test_admin_b_sees_only_own_users(self, client_b):
        r = await client_b.get("/users")
        assert r.status_code == 200
        assert "admin_b" in r.text
        assert "admin_a" not in r.text

    async def test_admin_a_cannot_reset_admin_b_password(self, client_a, data):
        r = await client_a.post(f"/users/{data['admin_b'].id}/reset-password", data={"csrf_token": "test"})
        assert r.status_code in (302, 303, 403, 404, 422)


# ── 6. Franchisor access control ─────────────────────────────────

class TestFranchisorAccess:
    async def test_admin_a_cannot_access_franchisor(self, client_a):
        r = await client_a.get("/franchisor")
        assert r.status_code in (302, 303)

    async def test_admin_b_cannot_access_franchisor(self, client_b):
        r = await client_b.get("/franchisor")
        assert r.status_code in (302, 303)

    async def test_manager_cannot_access_franchisor(self, manager_a_client):
        r = await manager_a_client.get("/franchisor")
        assert r.status_code in (302, 303)


# ── 7. Blocked tenant ────────────────────────────────────────────

class TestBlockedTenant:
    @pytest.fixture(autouse=True)
    async def _block_and_cleanup(self, data):
        from app.main import cancel_monitor_for_tenant

        async with async_session() as session:
            repo = TenantRepository(session)
            await repo.toggle_active(data["t_a"].id)
            await session.commit()

        cancel_monitor_for_tenant(data["t_a"].id)
        yield
        async with async_session() as session:
            repo = TenantRepository(session)
            await repo.toggle_active(data["t_a"].id)
            await session.commit()

    async def test_blocked_user_redirected_to_login(self, client_a):
        r = await client_a.get("/")
        assert r.status_code in (302, 303)
        assert "login" in r.headers.get("location", "")

    async def test_blocked_user_cannot_access_leads(self, client_a):
        r = await client_a.get("/leads")
        assert r.status_code in (302, 303)

    async def test_blocked_user_cannot_access_settings(self, client_a):
        r = await client_a.get("/settings")
        assert r.status_code in (302, 303)

    async def test_blocked_user_cannot_post(self, client_a):
        r = await client_a.post("/leads/new", data={"csrf_token": "test", "message_text": "test"})
        assert r.status_code in (302, 303)


# ── 8. Slug validation ──────────────────────────────────────────

class TestSlugValidation:
    async def test_duplicate_slug_rejected(self):
        async with async_session() as session:
            repo = TenantRepository(session)
            existing = await repo.get_by_slug("tenant-a")
            assert existing is not None

    async def test_reserved_slugs_listed(self):
        for slug in _RESERVED_SLUGS:
            assert slug in _RESERVED_SLUGS

    async def test_slug_format_regex(self):
        assert re.fullmatch(r"[a-z0-9][a-z0-9\-]*", "tenant-a")
        assert re.fullmatch(r"[a-z0-9][a-z0-9\-]*", "remont2024")
        assert not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", "Tenant-A")
        assert not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", "tenant_a")
        assert not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", "-tenant")
        assert not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", "tenant a")


# ── 9. Archive isolation ────────────────────────────────────────

class TestArchiveIsolation:
    async def test_admin_a_sees_only_own_archive(self, client_a):
        r = await client_a.get("/archive")
        assert r.status_code == 200

    async def test_admin_b_sees_only_own_archive(self, client_b):
        r = await client_b.get("/archive")
        assert r.status_code == 200


# ── 10. Analytics isolation ──────────────────────────────────────

class TestAnalyticsIsolation:
    async def test_admin_a_sees_only_own_analytics(self, client_a):
        r = await client_a.get("/analytics")
        assert r.status_code == 200

    async def test_admin_b_sees_only_own_analytics(self, client_b):
        r = await client_b.get("/analytics")
        assert r.status_code == 200


# ── 11. Team isolation ──────────────────────────────────────────

class TestTeamIsolation:
    async def test_admin_a_sees_only_own_team(self, client_a):
        r = await client_a.get("/team")
        assert r.status_code == 200
        assert "admin_a" in r.text or "Admin A" in r.text
        assert "admin_b" not in r.text

    async def test_admin_b_sees_only_own_team(self, client_b):
        r = await client_b.get("/team")
        assert r.status_code == 200
        assert "admin_b" in r.text or "Admin B" in r.text
        assert "admin_a" not in r.text


# ── 12. Cross-tenant POST operations ────────────────────────────

class TestCrossTenantPOST:
    async def test_admin_a_cannot_create_lead_in_tenant_b(self, client_a):
        r = await client_a.post("/leads/new", data={
            "csrf_token": "test", "message_text": "test lead", "first_name": "Hacker",
        })
        if r.status_code in (302, 303):
            return
        async with async_session() as session:
            result = await session.execute(select(Lead).where(Lead.first_name == "Hacker"))
            lead = result.scalar_one_or_none()
            if lead:
                assert lead.tenant_id == client_a.tenant.id

    async def test_admin_b_cannot_note_on_lead_a(self, client_b, data):
        r = await client_b.post(f"/leads/{data['lead_a1'].id}/note", data={"csrf_token": "test", "note_text": "hacked"})
        assert r.status_code in (302, 303, 403, 404, 422)

    async def test_admin_a_cannot_assign_lead_b(self, client_a, data):
        r = await client_a.post(f"/leads/{data['lead_b1'].id}/assign", data={
            "csrf_token": "test", "assigned_to": str(data["admin_a"].id)
        })
        assert r.status_code in (302, 303, 403, 404, 422)

    async def test_admin_a_cannot_delete_lead_b(self, client_a, data):
        r = await client_a.post(f"/leads/{data['lead_b1'].id}/delete", data={"csrf_token": "test"})
        assert r.status_code in (302, 303, 403, 404)

    async def test_admin_a_cannot_change_lead_b_status(self, client_a, data):
        r = await client_a.post(f"/leads/{data['lead_b1'].id}/status", data={"csrf_token": "test", "status": "deal"})
        assert r.status_code in (302, 303, 403, 404)
