import sqlite3
import os
import time
import hmac
import logging
from datetime import datetime, timedelta
from flask import request, current_app, abort
from flask_login import UserMixin, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

logger = logging.getLogger(__name__)

# Re-use project DB_PATH
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_ROOT, "data", "alcosoft.db")
AUDIT_LOG_PATH = os.path.join(_ROOT, "data", "auth_audit.log")

# ── Brute-force protection (P3-Q3) ───────────────────────────
# In-memory throttle keyed by (username, ip). Not distributed, but the dashboard
# is a single Flask process, so this is sufficient to stop credential stuffing.
_login_attempts: dict = {}
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_LOCKOUT_SEC = 300  # 5 minutes


def _login_key() -> str:
    ip = request.remote_addr if request else "unknown"
    return ip


def is_login_locked() -> tuple[bool, int]:
    rec = _login_attempts.get(_login_key())
    if not rec:
        return False, 0
    locked_until = rec.get("locked_until", 0)
    remaining = int(locked_until - time.time())
    if remaining > 0:
        return True, remaining
    return False, 0


def _register_login_failure():
    key = _login_key()
    rec = _login_attempts.get(key, {"count": 0, "locked_until": 0})
    rec["count"] = rec.get("count", 0) + 1
    if rec["count"] >= _MAX_LOGIN_ATTEMPTS:
        rec["locked_until"] = time.time() + _LOGIN_LOCKOUT_SEC
        rec["count"] = 0
        logger.warning("🔒 Login locked for %s for %ss (too many failures)", key, _LOGIN_LOCKOUT_SEC)
    _login_attempts[key] = rec


def _register_login_success():
    _login_attempts.pop(_login_key(), None)

def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_auth_db():
    try:
        with _get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to init auth DB: {e}")

def log_auth_event(event_type: str, username: str, success: bool, details: str = ""):
    timestamp = datetime.now().isoformat()
    ip = request.remote_addr if request else "unknown"
    status = "SUCCESS" if success else "FAILED"
    log_entry = f"[{timestamp}] [{ip}] {event_type} | User: {username} | Status: {status} | {details}\n"
    
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(log_entry)
    except Exception as e:
        logger.error(f"Failed to write to auth audit log: {e}")

class User(UserMixin):
    def __init__(self, id, username, role, created_at):
        self.id = id
        self.username = username
        self.role = role
        self.created_at = created_at
        
    @property
    def is_expired_emergency_admin(self):
        if self.role != 'emergency_admin':
            return False
        # 24h expiry
        created = datetime.fromisoformat(self.created_at)
        if datetime.now() > created + timedelta(hours=24):
            return True
        return False

def load_user(user_id):
    try:
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row:
                user = User(row['id'], row['username'], row['role'], row['created_at'])
                # Auto-delete expired emergency admins when they try to load
                if user.is_expired_emergency_admin:
                    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                    conn.commit()
                    log_auth_event("EXPIRE_EMERGENCY_ADMIN", user.username, True, "Auto-deleted expired account.")
                    return None
                return user
    except Exception as e:
        logger.error(f"Error loading user: {e}")
    return None

def authenticate_user(username, password):
    # P3-Q3: brute-force lockout
    locked, remaining = is_login_locked()
    if locked:
        log_auth_event("LOGIN", username or "?", False, f"Locked out ({remaining}s remaining)")
        return None
    try:
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row and check_password_hash(row['password_hash'], password):
                user = User(row['id'], row['username'], row['role'], row['created_at'])
                if user.is_expired_emergency_admin:
                    conn.execute("DELETE FROM users WHERE id = ?", (row['id'],))
                    conn.commit()
                    log_auth_event("EXPIRE_EMERGENCY_ADMIN", username, True, "Auto-deleted expired account.")
                    return None
                _register_login_success()
                log_auth_event("LOGIN", username, True)
                return user
    except Exception as e:
        logger.error(f"Error authenticating user: {e}")

    _register_login_failure()
    log_auth_event("LOGIN", username, False, "Invalid credentials or non-existent user")
    return None


def is_trusted_local_admin_access():
    """Controlled break-glass local admin access.

    P3-Q3 FIX: the previous version granted FULL ADMIN with NO password to any
    request that merely appeared to originate from localhost — trivially abusable
    behind a misconfigured reverse proxy (no X-Forwarded-For) or via SSRF. It is
    now gated on a strong shared secret supplied in the X-Local-Admin-Token header,
    so the bypass is no longer passwordless.
    """
    if os.environ.get('LOCAL_ADMIN_BYPASS', 'false').lower() != 'true':
        return False
    if not request:
        return False

    expected = os.environ.get('LOCAL_ADMIN_BYPASS_TOKEN', '')
    if not expected or len(expected) < 16:
        logger.error(
            "LOCAL_ADMIN_BYPASS is enabled but LOCAL_ADMIN_BYPASS_TOKEN is unset or too "
            "weak (<16 chars). Refusing passwordless bypass."
        )
        return False

    provided = request.headers.get('X-Local-Admin-Token', '')
    if not provided or not hmac.compare_digest(str(provided), str(expected)):
        return False

    is_local = request.remote_addr in ['127.0.0.1', '::1']
    has_proxies = request.headers.get('X-Forwarded-For') or request.headers.get('X-Real-IP')
    if not (is_local and not has_proxies):
        return False

    logger.warning("🔓 LOCAL_ADMIN_BYPASS token accepted from %s", request.remote_addr)
    return True


def load_user_from_request(request):
    """Integrates with Flask-Login to auto-login trusted local requests (token-gated)."""
    if is_trusted_local_admin_access():
        return User(0, "LOCAL_BYPASS_ADMIN", "admin", datetime.now().isoformat())
    return None

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return current_app.login_manager.unauthorized()
            
        if current_user.role not in ['admin', 'emergency_admin']:
            log_auth_event("UNAUTHORIZED_ACCESS", current_user.username, False, f"Attempted to access admin-only route: {request.path}")
            abort(403)
            
        return f(*args, **kwargs)
    return decorated_function
