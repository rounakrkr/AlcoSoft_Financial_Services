# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/token_validator.py — JWT Token Shield
#   Pre-validates token scope & expiry BEFORE order execution
#   Prevents stCode=100008 (unauthorized) errors
# ============================================================

import logging
import json
import base64
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class TokenScope(Enum):
    """Valid JWT scopes from Kotak."""
    VIEW = "View"
    TRADE = "Trade"


class TokenState(Enum):
    """Token health status."""
    VALID = "VALID"              # ✅ Fully operational
    EXPIRING_SOON = "EXPIRING"   # ⏰ Within 5 min of expiry
    EXPIRED = "EXPIRED"          # ❌ Past exp timestamp
    WRONG_SCOPE = "WRONG_SCOPE"  # 🔐 Has View, needs Trade
    CORRUPTED = "CORRUPTED"      # 💥 Malformed JWT


class JWTTokenValidator:
    """
    Decodes and validates Kotak JWT tokens WITHOUT external libraries.
    Pure Base64 decoding + timestamp comparison.
    
    Security Note:
    - Does NOT verify JWT signature (Kotak's key unavailable)
    - Only validates local claims: exp, scope, iat
    - Used for operational health checks, not security validation
    """
    
    MIN_EXPIRY_BUFFER_SECONDS = 300  # Refresh if < 5 min until exp
    
    @staticmethod
    def decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
        """
        Decode JWT WITHOUT verification.
        Format: header.payload.signature
        Returns decoded payload dict or None if malformed.
        
        Example payload:
        {
          "jti": "...",
          "iss": "login-service",
          "sub": "...",
          "ucc": "XD16F",
          "scope": ["Trade"],
          "exp": 1779906600,
          "iat": 1779858307
        }
        """
        try:
            parts = token.split(".")
            if len(parts) != 3:
                logger.warning(f"Invalid JWT format (expected 3 parts, got {len(parts)})")
                return None
            
            # Decode payload (second part)
            payload_b64 = parts[1]
            # Add padding if needed (JWT strips trailing =)
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            
            payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
            payload = json.loads(payload_json)
            return payload
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"JWT decode failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected JWT decode error: {e}")
            return None
    
    @staticmethod
    def validate_token_state(token: Optional[str]) -> TokenState:
        """
        Check if token is ready for trade operations.
        Returns TokenState enum.
        """
        if not token or not isinstance(token, str):
            return TokenState.CORRUPTED
        
        payload = JWTTokenValidator.decode_jwt_payload(token)
        if not payload:
            return TokenState.CORRUPTED
        
        # ── Check Scope ──────────────────────────────────────────
        scope_list = payload.get("scope", [])
        if not isinstance(scope_list, list):
            scope_list = [scope_list]
        
        has_trade_scope = any(
            s.strip().upper() == TokenScope.TRADE.value.upper()
            for s in scope_list
        )
        
        if not has_trade_scope:
            logger.warning(
                f"⚠️ Token has WRONG SCOPE: {scope_list} "
                f"(expected ['Trade'])"
            )
            return TokenState.WRONG_SCOPE
        
        # ── Check Expiry ─────────────────────────────────────────
        exp = payload.get("exp")
        if not exp:
            logger.error("Token missing 'exp' claim")
            return TokenState.CORRUPTED
        
        try:
            exp_timestamp = int(exp)
        except (ValueError, TypeError):
            logger.error(f"Invalid exp claim: {exp}")
            return TokenState.CORRUPTED
        
        now_timestamp = int(time.time())
        time_until_exp = exp_timestamp - now_timestamp
        
        if time_until_exp < 0:
            logger.error(
                f"Token EXPIRED {abs(time_until_exp)}s ago "
                f"(exp={exp_timestamp}, now={now_timestamp})"
            )
            return TokenState.EXPIRED
        
        if time_until_exp < JWTTokenValidator.MIN_EXPIRY_BUFFER_SECONDS:
            logger.warning(
                f"Token EXPIRING SOON: {time_until_exp}s until expiry "
                f"(min buffer: {JWTTokenValidator.MIN_EXPIRY_BUFFER_SECONDS}s)"
            )
            return TokenState.EXPIRING_SOON
        
        pass
        return TokenState.VALID
    
    @staticmethod
    def get_token_info(token: Optional[str]) -> Dict[str, Any]:
        """
        Extract human-readable token info for logging.
        Safe to call even on invalid tokens.
        """
        if not token:
            return {"status": "NO_TOKEN"}
        
        payload = JWTTokenValidator.decode_jwt_payload(token)
        if not payload:
            return {"status": "CORRUPTED_TOKEN"}
        
        exp = payload.get("exp")
        iat = payload.get("iat")
        scope = payload.get("scope", [])
        ucc = payload.get("ucc", "N/A")
        
        try:
            time_until_exp = int(exp) - int(time.time()) if exp else -1
            exp_dt = datetime.fromtimestamp(int(exp)) if exp else None
            iat_dt = datetime.fromtimestamp(int(iat)) if iat else None
        except (ValueError, TypeError, OSError):
            time_until_exp = -999
            exp_dt = None
            iat_dt = None
        
        return {
            "status": "VALID" if time_until_exp > 0 else "EXPIRED",
            "scope": scope,
            "ucc": ucc,
            "expires_at": exp_dt.isoformat() if exp_dt else None,
            "issued_at": iat_dt.isoformat() if iat_dt else None,
            "seconds_until_expiry": max(0, time_until_exp),
            "created_seconds_ago": int(time.time()) - int(iat) if iat else None,
        }


# ════════════════════════════════════════════════════════════
#   PRE-ORDER TOKEN VALIDATION MIDDLEWARE
# ════════════════════════════════════════════════════════════

def validate_and_fix_session_before_order() -> bool:
    """
    Master pre-order checklist:
    1. Get current token from Kotak client
    2. Decode and check scope
    3. Check expiry
    4. If WRONG_SCOPE or EXPIRED → force fresh session
    5. Return True only if Trade-scoped token ready
    
    Call this RIGHT BEFORE place_order() call.
    """
    from core.kotak_client import get_client, force_reconnect
    
    try:
        client = get_client()
    except Exception as e:
        logger.error(f"❌ Cannot get Kotak client: {e}")
        return False
    
    # Try to extract current token from NeoAPI internals
    # (NeoAPI stores it in client.access_token or _access_token)
    current_token = _extract_token_from_client(client)
    
    if not current_token:
        logger.warning("⚠️ No token in client object — may not be authenticated yet")
        return False
    
    # ── Validate Token State ──────────────────────────────────────
    token_state = JWTTokenValidator.validate_token_state(current_token)
    token_info = JWTTokenValidator.get_token_info(current_token)
    
    logger.info(
        f"🔐 Pre-Order Token Check | State: {token_state.value} | "
        f"Info: {token_info}"
    )
    
    # ── Handle Each State ─────────────────────────────────────────
    if token_state == TokenState.VALID:
        logger.info("✅ Token ready for order — proceeding")
        return True
    
    elif token_state == TokenState.EXPIRING_SOON:
        logger.warning(
            f"⏰ Token expiring soon ({token_info.get('seconds_until_expiry')}s) "
            f"— refreshing preemptively"
        )
        return _force_session_refresh("token_expiring_soon")
    
    elif token_state == TokenState.EXPIRED:
        logger.error("❌ Token EXPIRED — forcing fresh session")
        return _force_session_refresh("token_expired")
    
    elif token_state == TokenState.WRONG_SCOPE:
        logger.error(
            f"🔐 Token has wrong scope {token_info.get('scope')} "
            f"(need ['Trade']) — forcing MPIN re-validation"
        )
        return _force_session_refresh("wrong_scope")
    
    else:  # CORRUPTED
        logger.error("💥 Token is corrupted — forcing fresh session")
        return _force_session_refresh("corrupted_token")


def _extract_token_and_sid_from_client(client) -> tuple[Optional[str], Optional[str]]:
    """
    🔐 CRITICAL: Extract BOTH token AND sid from NeoAPI client.
    Returns (token, sid) tuple. Tries multiple attribute names.
    """
    # Extract token
    token_attrs = [
        "access_token",      # Standard
        "_access_token",     # Private
        "token",             # Generic
        "_token",            # Private
    ]
    
    token = None
    for attr in token_attrs:
        try:
            val = getattr(client, attr, None)
            if val and isinstance(val, str) and len(val) > 50:
                token = val
                break
        except Exception:
            pass
    
    # Extract sid
    sid = None
    sid_attrs = [
        "sid",
        "session_id",
        "_sid",
        "_session_id",
    ]
    
    for attr in sid_attrs:
        try:
            val = getattr(client, attr, None)
            if val and isinstance(val, str) and len(val) > 10:
                sid = val
                break
        except Exception:
            pass
    
    # Also try to extract from configuration if already set
    if not sid:
        try:
            if hasattr(client, 'api_client') and hasattr(client.api_client, 'configuration'):
                sid = getattr(client.api_client.configuration, 'edit_sid', None)
                if sid and isinstance(sid, str):
                    logger.debug(f"Found sid in client.api_client.configuration.edit_sid")
        except Exception:
            pass
    
    if not token:
        logger.warning("Could not extract token from NeoAPI client")
    if not sid:
        logger.warning("Could not extract sid from NeoAPI client")
    
    return token, sid


def _extract_token_from_client(client) -> Optional[str]:
    """
    Safely extract token from NeoAPI client.
    Tries multiple attribute names (library may change internals).
    """
    token, _ = _extract_token_and_sid_from_client(client)
    return token


def _force_session_refresh(reason: str) -> bool:
    """
    Destroys old session and creates fresh one.
    Returns True only if new session is Trade-scoped and valid.
    """
    from core.kotak_client import force_reconnect
    
    logger.warning(f"🔄 Forcing session refresh (reason: {reason})")
    
    try:
        new_client = force_reconnect()
        logger.info("✅ Fresh session created")
        
        # Validate new session
        new_token = _extract_token_from_client(new_client)
        if not new_token:
            logger.error("❌ Fresh session has no token")
            return False
        
        new_state = JWTTokenValidator.validate_token_state(new_token)
        if new_state == TokenState.VALID:
            logger.info("✅ New session is Trade-scoped and valid")
            return True
        else:
            logger.error(f"❌ New session invalid: {new_state.value}")
            return False
    
    except Exception as e:
        logger.error(f"❌ Session refresh failed: {e}")
        return False


# ════════════════════════════════════════════════════════════
#   DIAGNOSTIC EXPORTS
# ════════════════════════════════════════════════════════════

def diagnose_token_health() -> Dict[str, Any]:
    """
    Full diagnostic report. Use in health checks / debugging.
    Safe to call — won't crash on bad tokens.
    """
    from core.kotak_client import get_client
    
    try:
        client = get_client()
    except Exception as e:
        return {
            "error": f"Cannot get client: {e}",
            "timestamp": datetime.now().isoformat(),
        }
    
    token = _extract_token_from_client(client)
    if not token:
        return {
            "status": "NOT_AUTHENTICATED",
            "timestamp": datetime.now().isoformat(),
        }
    
    token_state = JWTTokenValidator.validate_token_state(token)
    token_info = JWTTokenValidator.get_token_info(token)
    
    return {
        "token_state": token_state.value,
        "token_info": token_info,
        "ready_for_trading": token_state == TokenState.VALID,
        "timestamp": datetime.now().isoformat(),
    }


def ensure_trade_token_on_client():
    """
    🔐 CRITICAL: Guarantees that client.access_token is Trade-scoped.
    
    This addresses the token scope degradation bug where NeoAPI library
    may hold stale View tokens internally.
    
    Call this IMMEDIATELY BEFORE client.place_order() to ensure correct token usage.
    
    Returns:
        - NeoAPI client with GUARANTEED Trade token set
        - Raises RuntimeError if Trade token cannot be obtained
    """
    from core.kotak_client import get_client, force_reconnect
    
    try:
        client = get_client()
    except Exception as e:
        logger.error(f"❌ Cannot get Kotak client: {e}")
        raise RuntimeError(f"Failed to get client: {e}")
    
    # 🔥 CRITICAL FIX: Extract from configuration FIRST, not from client object
    # The client.access_token property might have stale View token!
    current_token = None
    if hasattr(client, 'api_client') and hasattr(client.api_client, 'configuration'):
        current_token = getattr(client.api_client.configuration, 'access_token', None) or getattr(client.api_client.configuration, 'edit_token', None)
    
    # Fallback: extract from client object only if config is empty
    if not current_token:
        pass
        current_token = _extract_token_from_client(client)
    
    if not current_token:
        logger.warning("⚠️ No token in client — forcing fresh session")
        try:
            client = force_reconnect()
            # 🔥 FIX: After reconnect, extract from configuration (fresh Trade token),
            # NOT from client.access_token which might be stale View token!
            if hasattr(client, 'api_client') and hasattr(client.api_client, 'configuration'):
                current_token = getattr(client.api_client.configuration, 'access_token', None) or getattr(client.api_client.configuration, 'edit_token', None)
            if not current_token:
                current_token = _extract_token_from_client(client)  # Fallback
            if not current_token:
                raise RuntimeError("No token after reconnect")
        except Exception as e:
            logger.error(f"❌ Cannot obtain token: {e}")
            raise RuntimeError(f"Token recovery failed: {e}")
    
    # Validate token state
    token_state = JWTTokenValidator.validate_token_state(current_token)
    token_info = JWTTokenValidator.get_token_info(current_token)
    
    pass
    
    # If NOT valid, force refresh
    if token_state != TokenState.VALID:
        logger.warning(
            f"⚠️ Token state {token_state.value} — forcing fresh session"
        )
        try:
            client = force_reconnect()
            # 🔥 FIX: After reconnect, extract from configuration (fresh Trade token),
            # NOT from client.access_token which might be stale View token!
            if hasattr(client, 'api_client') and hasattr(client.api_client, 'configuration'):
                current_token = getattr(client.api_client.configuration, 'access_token', None) or getattr(client.api_client.configuration, 'edit_token', None)
            if not current_token:
                current_token = _extract_token_from_client(client)  # Fallback
            token_state = JWTTokenValidator.validate_token_state(current_token)
            if token_state != TokenState.VALID:
                raise RuntimeError(f"Token still invalid after reconnect: {token_state.value}")
        except Exception as e:
            logger.error(f"❌ Token refresh failed: {e}")
            raise RuntimeError(f"Cannot obtain valid token: {e}")
    
    # CRITICAL: Explicitly re-set token AND sid on client
    # This ensures NeoAPI uses BOTH properties for place_order()
    try:
        pass
        client.access_token = current_token
        
        # 🔑 CRITICAL: Extract AND set BOTH edit_token and edit_sid
        # place_order() checks: if self.configuration.edit_token and self.configuration.edit_sid:
        _, current_sid = _extract_token_and_sid_from_client(client)
        
        if hasattr(client, 'api_client') and hasattr(client.api_client, 'configuration'):
            # 🔥 CRITICAL: Set BOTH access_token AND edit_token for Authorization header
            # NeoAPI library uses access_token for "Authorization: Bearer {access_token}" header
            client.api_client.configuration.access_token = current_token
            client.api_client.configuration.edit_token = current_token
            
            # 🔥 CRITICAL FIX: ALWAYS set edit_sid, even if we have to use client.sid
            if current_sid:
                client.api_client.configuration.edit_sid = current_sid
                pass
            elif hasattr(client, 'sid') and client.sid:
                client.api_client.configuration.edit_sid = client.sid
                pass
            else:
                logger.warning(f"⚠️ Could not find sid to set edit_sid!")
            
            # 🔥 CRITICAL: Also verify baseUrl is set (Trade endpoint)
            current_host = getattr(client.api_client.configuration, 'host', None)
            if current_host:
                pass
            else:
                logger.warning(f"⚠️ No baseUrl on configuration.host - place_order() might use wrong endpoint!")
        
        logger.info(f"✅ Trade token + sid confirmed and set on client (scope=['Trade'])")
    except Exception as e:
        logger.error(f"❌ Failed to set token/sid on client: {e}")
        raise RuntimeError(f"Cannot set token/sid on client: {e}")
    
    return client


if __name__ == "__main__":
    pass
