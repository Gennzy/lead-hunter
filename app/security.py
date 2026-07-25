import re
import secrets
import hashlib
import time
from functools import wraps
from pathlib import Path
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse

CSRF_TOKEN_EXPIRY = 3600
MAX_REQUESTS_PER_MINUTE = 60

# NOTE: These are in-memory dicts. Works for single-worker deployment.
# For multi-worker (Gunicorn -w N), move to Redis/DB for shared state.
# Otherwise CSRF tokens and rate limits won't be shared between workers.
_rate_limit_store: dict[str, list[float]] = {}
_csrf_tokens: dict[str, float] = {}


def generate_secret_key() -> str:
    return secrets.token_hex(32)


def generate_csrf_token(session_id: str = "default") -> str:
    token = secrets.token_hex(32)
    _csrf_tokens[token] = time.time()
    # Opportunistic cleanup — without this, tokens that are generated but
    # never submitted (e.g. abandoned forms, bots) accumulate forever.
    if len(_csrf_tokens) > 2000:
        now = time.time()
        expired = [t for t, created in _csrf_tokens.items() if now - created > CSRF_TOKEN_EXPIRY]
        for t in expired:
            _csrf_tokens.pop(t, None)
    return token


def validate_csrf_token(token: str) -> bool:
    if not token or token not in _csrf_tokens:
        return False
    created = _csrf_tokens.pop(token)
    return (time.time() - created) < CSRF_TOKEN_EXPIRY


def get_client_ip(request: Request) -> str:
    # IMPORTANT: X-Forwarded-For is only trustworthy when a reverse proxy
    # (nginx/traefik/Cloudflare) sits in front and *overwrites* this header
    # itself. There is currently no such proxy in docker-compose.yml, which
    # means any client can set this header directly and pick a fresh
    # "IP" on every request — completely bypassing rate limiting and
    # login brute-force protection. Use the real socket peer address until
    # a trusted proxy is actually in place; if one is added later, this
    # should read only the hop that proxy appends, not blindly trust the
    # whole header.
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request, limit: int = MAX_REQUESTS_PER_MINUTE) -> bool:
    ip = get_client_ip(request)
    now = time.time()
    window = 60

    if ip not in _rate_limit_store:
        _rate_limit_store[ip] = []

    _rate_limit_store[ip] = [t for t in _rate_limit_store[ip] if now - t < window]

    if len(_rate_limit_store[ip]) >= limit:
        return False

    _rate_limit_store[ip].append(now)
    return True


def sanitize_input(text: str, max_length: int = 5000) -> str:
    """Strip potentially dangerous HTML tags. Case-insensitive.
    
    NOTE: This is a second line of defense — Jinja2 escapes on render.
    Do NOT rely on this as sole XSS protection.
    """
    if not text:
        return ""
    text = text.strip()
    text = text[:max_length]
    dangerous = [r'<script', r'javascript:', r'onerror=', r'onload=', r'onclick=']
    for d in dangerous:
        text = re.sub(d, '', text, flags=re.IGNORECASE)
    return text


def setup_security(app):
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if not check_rate_limit(request):
            return HTMLResponse(
                content="Rate limit exceeded. Try again later.",
                status_code=429,
            )

        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        return response


def protect_env_file():
    env_path = Path(".env")
    if env_path.exists():
        try:
            import stat
            env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass

    session_files = Path(".").glob("*.session*")
    for sf in session_files:
        try:
            import stat
            sf.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
