import sqlite3
import os
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
                log_auth_event("LOGIN", username, True)
                return user
    except Exception as e:
        logger.error(f"Error authenticating user: {e}")
    
    log_auth_event("LOGIN", username, False, "Invalid credentials or non-existent user")
    return None

def is_trusted_local_admin_access():
    """Future-proof local detection for anti-lockout bypass."""
    bypass_enabled = os.environ.get('LOCAL_ADMIN_BYPASS', 'false').lower() == 'true'
    if not bypass_enabled:
        return False
        
    if not request:
        return False

    is_local = request.remote_addr in ['127.0.0.1', '::1', 'localhost']
    has_proxies = request.headers.get('X-Forwarded-For') or request.headers.get('X-Real-IP')
    
    return is_local and not has_proxies

def load_user_from_request(request):
    """Integrates with Flask-Login to auto-login trusted local requests."""
    if is_trusted_local_admin_access():
        # Inject a virtual Admin user globally
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
