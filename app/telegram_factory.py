from __future__ import annotations

import asyncio
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, connection
import telethon.errors
from sqlalchemy import select, update

from app.models import TelegramSession, Tenant, async_session
from config import settings

logger = logging.getLogger(__name__)

# Configurable from .env — defaults to 5
_MAX_CONNECTIONS = settings.max_telegram_connections

_CONNECT_SEMAPHORE = asyncio.Semaphore(_MAX_CONNECTIONS)

# Session files directory — restricted permissions (absolute path)
_SESSIONS_DIR = Path(__file__).resolve().parent.parent / "telegram_sessions"
_SESSIONS_DIR.mkdir(exist_ok=True)

# Rate limiting for authorization attempts
_AUTH_ATTEMPTS: dict[int, list[float]] = {}
_AUTH_MAX_ATTEMPTS = 5
_AUTH_WINDOW_SECONDS = 300  # 5 minutes


def _get_session_path(tenant_id: int) -> str:
    """Build session file path from tenant_id only — no user input."""
    safe_name = f"tenant_{tenant_id}"
    session_path = _SESSIONS_DIR / safe_name
    return str(session_path)


def _restrict_session_dir():
    """Restrict permissions on sessions directory (Unix only)."""
    try:
        if os.name != 'nt':  # Skip on Windows
            os.chmod(_SESSIONS_DIR, stat.S_IRWXU)  # 700 — owner only
    except OSError:
        pass


def _restrict_session_file(path: str):
    """Restrict permissions on a session file (Unix only)."""
    try:
        if os.name != 'nt':  # Skip on Windows
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600 — owner read/write only
    except OSError:
        pass


def _check_rate_limit(tenant_id: int) -> bool:
    """Check if tenant is within authorization rate limits."""
    now = datetime.now(timezone.utc).timestamp()
    if tenant_id not in _AUTH_ATTEMPTS:
        _AUTH_ATTEMPTS[tenant_id] = []

    # Clean old attempts
    _AUTH_ATTEMPTS[tenant_id] = [
        t for t in _AUTH_ATTEMPTS[tenant_id]
        if now - t < _AUTH_WINDOW_SECONDS
    ]

    if len(_AUTH_ATTEMPTS[tenant_id]) >= _AUTH_MAX_ATTEMPTS:
        return False

    _AUTH_ATTEMPTS[tenant_id].append(now)
    return True


class TelegramClientFactory:
    """Manages multiple Telethon clients, one per tenant."""

    def __init__(self):
        self._clients: dict[int, TelegramClient] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._phone_code_hashes: dict[int, str] = {}
        _restrict_session_dir()

    def _get_api_credentials(self, tenant_config=None) -> tuple[str, str]:
        """Get API credentials from tenant config or global settings."""
        if tenant_config:
            api_id = tenant_config._config.get("telegram_api_id", settings.telegram_api_id)
            api_hash = tenant_config._config.get("telegram_api_hash", settings.telegram_api_hash)
            return str(api_id), api_hash
        return str(settings.telegram_api_id), settings.telegram_api_hash

    def create_client(self, tenant_id: int, session_name: str,
                      tenant_config=None) -> TelegramClient:
        """Create or get existing client for a tenant."""
        if tenant_id in self._clients:
            return self._clients[tenant_id]

        # Always use secure path from tenant_id, ignore user-provided session_name
        secure_path = _get_session_path(tenant_id)

        api_id, api_hash = self._get_api_credentials(tenant_config)

        client = TelegramClient(secure_path, int(api_id), api_hash)
        self._clients[tenant_id] = client

        # NOTE: Don't restrict session file here - SQLite creates it on connect()
        # Restricting before SQLite finishes writing causes "readonly database" error
        return client

    def get_client(self, tenant_id: int) -> Optional[TelegramClient]:
        """Get existing client for a tenant."""
        return self._clients.get(tenant_id)

    async def start_client(self, tenant_id: int) -> bool:
        """Start a client connection."""
        client = self._clients.get(tenant_id)
        if not client:
            return False

        async with _CONNECT_SEMAPHORE:
            try:
                if not client.is_connected():
                    await client.start()
                # Update last_active
                async with async_session() as session:
                    await session.execute(
                        update(TelegramSession)
                        .where(TelegramSession.tenant_id == tenant_id)
                        .values(last_active=datetime.now(timezone.utc))
                    )
                    await session.commit()

                # Restrict session file permissions after connection
                _restrict_session_file(_get_session_path(tenant_id) + ".session")

                return True
            except Exception as e:
                logger.error("Failed to start client for tenant %d: %s", tenant_id, e)
                return False

    async def stop_client(self, tenant_id: int):
        """Stop a client connection."""
        client = self._clients.pop(tenant_id, None)
        task = self._tasks.pop(tenant_id, None)
        if task and not task.done():
            task.cancel()
        if client and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass

    async def stop_all(self):
        """Stop all clients."""
        for tenant_id in list(self._clients.keys()):
            await self.stop_client(tenant_id)

    def is_running(self, tenant_id: int) -> bool:
        """Check if a client is running."""
        return tenant_id in self._clients and self._clients[tenant_id].is_connected()

    def get_running_tenants(self) -> list[int]:
        """Get list of tenant IDs with running clients."""
        return [tid for tid, c in self._clients.items() if c.is_connected()]

    async def authorize(self, tenant_id: int, phone: str) -> dict:
        """Start authorization process for a tenant."""
        if not _check_rate_limit(tenant_id):
            return {"error": "Too many attempts. Wait 5 minutes."}

        client = self._clients.get(tenant_id)
        if not client:
            return {"error": "Client not found"}

        try:
            await client.start(phone=phone)
            # Update session status
            async with async_session() as session:
                await session.execute(
                    update(TelegramSession)
                    .where(TelegramSession.tenant_id == tenant_id)
                    .values(is_authorized=True, phone_number=phone,
                            last_active=datetime.now(timezone.utc))
                )
                await session.commit()

            _restrict_session_file(_get_session_path(tenant_id) + ".session")

            return {"status": "authorized"}
        except Exception as e:
            logger.error("Authorization failed for tenant %d: %s", tenant_id, e)
            return {"error": str(e)}

    async def send_code(self, tenant_id: int, phone: str) -> dict:
        """Send verification code."""
        if not _check_rate_limit(tenant_id):
            return {"error": "Too many attempts. Wait 5 minutes."}

        client = self._clients.get(tenant_id)
        if not client:
            return {"error": "Client not found"}

        try:
            if client.is_connected():
                await client.disconnect()

            # Remove client from cache so create_client makes a fresh one
            self._clients.pop(tenant_id, None)

            # Delete ALL session-related files (session, journal, wal, shm)
            prefix = _get_session_path(tenant_id)
            for suffix in ['', '.session', '-journal', '-wal', '-shm']:
                p = Path(prefix + suffix)
                if p.exists():
                    p.unlink()
                    logger.info("Deleted: %s", p)

            # Create fresh client
            from app.config_manager import TenantConfig
            tenant_config = await TenantConfig.create(tenant_id)
            client = self.create_client(tenant_id, "", tenant_config)

            logger.info("Connecting to Telegram (session: %s.session)", prefix)
            await client.connect()
            logger.info("Connected to Telegram for tenant %d", tenant_id)

            # Now restrict session file permissions after SQLite has finished writing
            _restrict_session_file(prefix + ".session")

            from telethon.tl.functions.auth import SendCodeRequest
            from telethon.tl.types import CodeSettings

            api_id, api_hash = self._get_api_credentials()
            result = await client(SendCodeRequest(
                phone_number=phone,
                api_id=int(api_id),
                api_hash=api_hash,
                settings=CodeSettings()
            ))
            self._phone_code_hashes[tenant_id] = result.phone_code_hash
            return {"status": "code_sent", "phone_code_hash": result.phone_code_hash}
        except Exception as e:
            logger.error("Send code failed for tenant %d: %s", tenant_id, e, exc_info=True)
            return {"error": str(e)}

    async def sign_in(self, tenant_id: int, phone: str, code: str,
                      phone_code_hash: str = None) -> dict:
        """Sign in with code."""
        if not _check_rate_limit(tenant_id):
            return {"error": "Too many attempts. Wait 5 minutes."}

        client = self._clients.get(tenant_id)
        if not client:
            return {"error": "Client not found"}

        if not phone_code_hash:
            phone_code_hash = self._phone_code_hashes.get(tenant_id)

        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            async with async_session() as session:
                await session.execute(
                    update(TelegramSession)
                    .where(TelegramSession.tenant_id == tenant_id)
                    .values(is_authorized=True, phone_number=phone,
                            last_active=datetime.now(timezone.utc))
                )
                await session.commit()

            _restrict_session_file(_get_session_path(tenant_id) + ".session")

            return {"status": "authorized"}
        except telethon.errors.SessionPasswordNeededError:
            return {"status": "password_needed"}
        except Exception as e:
            logger.error("Sign in failed for tenant %d: %s", tenant_id, e)
            return {"error": str(e)}

    async def sign_in_password(self, tenant_id: int, password: str) -> dict:
        """Sign in with 2FA password."""
        if not _check_rate_limit(tenant_id):
            return {"error": "Too many attempts. Wait 5 minutes."}

        client = self._clients.get(tenant_id)
        if not client:
            return {"error": "Client not found"}

        try:
            await client.sign_in(password=password)
            async with async_session() as session:
                await session.execute(
                    update(TelegramSession)
                    .where(TelegramSession.tenant_id == tenant_id)
                    .values(is_authorized=True,
                            last_active=datetime.now(timezone.utc))
                )
                await session.commit()

            _restrict_session_file(_get_session_path(tenant_id) + ".session")

            return {"status": "authorized"}
        except Exception as e:
            logger.error("2FA sign in failed for tenant %d: %s", tenant_id, e)
            return {"error": str(e)}


# Global factory instance
client_factory = TelegramClientFactory()
