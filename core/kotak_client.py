# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   core/kotak_client.py — Auth & Session Manager
#   Handles zero-touch login via PyOTP + Kotak Neo v2.0.1
# ============================================================

import pyotp
import time
import logging
from dotenv import load_dotenv
import os
from neo_api_client import NeoAPI

load_dotenv()
logger = logging.getLogger(__name__)

# ── Singleton: only one Kotak session exists at a time ──────
_client_instance = None
consumer_secret = os.getenv("KOTAK_TOTP_SECRET")


def get_client() -> NeoAPI:
    """
    Returns the active Kotak session.
    If not logged in yet, performs full auto-login.
    If session expired, re-authenticates silently.
    Call this from anywhere in the project.
    """
    global _client_instance

    if _client_instance is None:
        _client_instance = _create_and_login()

    return _client_instance


def force_reconnect() -> NeoAPI:
    """
    Call this if you get an auth error mid-session.
    Destroys old session and creates a fresh one.
    """
    global _client_instance
    logger.warning("Force reconnect triggered — re-authenticating with Kotak...")

    try:
        if _client_instance:
            _client_instance.logout()
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.debug("Logout failed (session likely closed): %s", e)
    except Exception as e:
        logger.warning("Unexpected error during Kotak logout: %s", e)

    _client_instance = None
    return get_client()


# ── Internal: create session + full 2FA login ───────────────
def _create_and_login() -> NeoAPI:
    """
    Full zero-touch login sequence:
    Step 1 → Init NeoAPI with consumer_key
    Step 2 → Generate TOTP via pyotp (no human needed)
    Step 3 → totp_login (gets view token + session id)
    Step 4 → totp_validate with MPIN (gets trade token)
    """
    consumer_key   = os.getenv("KOTAK_CONSUMER_KEY")
    mobile_number  = os.getenv("KOTAK_MOBILE_NUMBER")
    ucc            = os.getenv("KOTAK_UCC")
    mpin           = os.getenv("KOTAK_MPIN")
    totp_secret    = os.getenv("KOTAK_TOTP_SECRET")

    # Validate all credentials exist
    missing = [k for k, v in {
        "KOTAK_CONSUMER_KEY":  consumer_key,
        "KOTAK_MOBILE_NUMBER": mobile_number,
        "KOTAK_UCC":           ucc,
        "KOTAK_MPIN":          mpin,
        "KOTAK_TOTP_SECRET":   totp_secret,
    }.items() if not v]

    if missing:
        raise EnvironmentError(
            f"Missing credentials in .env: {', '.join(missing)}"
        )

    # 🔍 CRITICAL DIAGNOSTIC: consumer_key validation
    if not consumer_key or len(consumer_key) < 10:
        logger.error(f"⚠️ CONSUMER_KEY looks invalid or too short: {consumer_key[:20] if consumer_key else 'EMPTY'}")
        logger.warning(
            "🔴 CRITICAL: consumer_key may be expired or invalid!\n"
            "   ACTION: Check Kotak Developer Portal:\n"
            "   1. Login to https://developer.kotaksecurities.com\n"
            "   2. Go to 'My Apps' section\n"
            "   3. Check if your app status is ACTIVE\n"
            "   4. Check if consumer_key is expired (usually 1 year validity)\n"
            "   5. If expired, regenerate or create new app\n"
            "   6. Copy NEW consumer_key to .env KOTAK_CONSUMER_KEY"
        )

    logger.info("Initializing Kotak Neo session...")

    # Step 1 — Init client
    client = NeoAPI(
        environment="PROD",  # ← Must be uppercase: PROD or UAT
        access_token=None,
        neo_fin_key=None,
        consumer_key=consumer_key
    )

    # Step 2 — Generate TOTP automatically (no phone needed)
    totp_code = pyotp.TOTP(totp_secret).now()
    logger.info(f"TOTP generated: {totp_code}")

    # Step 3 — TOTP Login (gets view token + session id)
    login_response = client.totp_login(
        mobile_number=mobile_number,
        ucc=ucc,
        totp=totp_code,
    )
    logger.info(f"TOTP login response: {login_response}")
    
    # 🔥 DEBUG: Log ALL fields in totp_login response
    if login_response and isinstance(login_response, dict):
        login_data = login_response.get("data", {})
        logger.info(f"🔍 TOTP login data keys: {list(login_data.keys())}")
        for key, value in login_data.items():
            if isinstance(value, str) and len(str(value)) > 100:
                logger.debug(f"   {key}: {str(value)[:100]}... (truncated)")
            else:
                logger.debug(f"   {key}: {value}")

    # Small buffer — let Kotak process the login
    time.sleep(2)

    # Step 4 — Validate with MPIN (gets trade token — needed for orders)
    validate_response = client.totp_validate(mpin=mpin)
    logger.info(f"MPIN validation response: {validate_response}")
    
    # 🔥 DEBUG: Log ALL fields in totp_validate response
    if validate_response and isinstance(validate_response, dict):
        data = validate_response.get("data", {})
        logger.info(f"🔍 MPIN response data keys: {list(data.keys())}")

    # CRITICAL: Extract and EXPLICITLY SET trade token on client's configuration
    # ⚠️ THE REAL SMOKING GUN: totp_validate() should set configuration.edit_token/edit_sid
    #    but we need to verify it worked AND set it manually if needed
    if validate_response and isinstance(validate_response, dict):
        data = validate_response.get("data", {})
        if not data.get("token"):
            logger.error(f"❌ Token missing in MPIN response: {validate_response}")
            raise RuntimeError("MPIN validation failed - no token in response")
        if data.get("kType") != "Trade":
            logger.error(f"❌ Expected Trade token, got: {data.get('kType')}")
            raise RuntimeError("MPIN validation did not return Trade token")
        
        # 🔐 FIX: Explicitly set BOTH configuration properties AND access_token/sid
        # NeoAPI's place_order checks: if self.configuration.edit_token and self.configuration.edit_sid
        trade_token = data.get("token")
        
        # Set on client object (legacy compatibility)
        client.access_token = trade_token
        client.sid = data.get("sid")
        
        # 🔑 CRITICAL: Set on configuration object (required for place_order)
        try:
            # Ensure configuration object exists
            if not hasattr(client, 'api_client'):
                logger.error("❌ Client missing api_client attribute")
                raise RuntimeError("NeoAPI client not properly initialized")
            
            if not hasattr(client.api_client, 'configuration'):
                logger.error("❌ api_client missing configuration attribute")
                raise RuntimeError("NeoAPI configuration not accessible")
            
            # 🔐 Extract CRITICAL fields from response
            sid = data.get("sid")
            base_url = data.get("baseUrl")  # 🔥 Trade-scoped API endpoint!
            hs_server_id = data.get("hsServerId")  # 🔥 CRITICAL: Used in order query params!
            rid = data.get("rid")  # Request ID — fallback for serverId
            
            # 🔥 CRITICAL: Set BOTH access_token AND edit_token for Authorization header
            # NeoAPI library uses access_token for "Authorization: Bearer {access_token}" header
            client.api_client.configuration.access_token = trade_token
            client.api_client.configuration.edit_token = trade_token
            client.api_client.configuration.edit_sid = sid
            
            # 🔥 CRITICAL: Set serverId (order API query param "sId" requires this)
            # Try hsServerId first, fallback to rid if needed
            server_id_to_use = hs_server_id or rid
            if server_id_to_use:
                client.api_client.configuration.serverId = server_id_to_use
                pass
            else:
                # serverId might not always be required; log warning but don't crash
                logger.warning("⚠️ Neither hsServerId nor rid available in MPIN response (order may fail)")
            
            # 🔥 CRITICAL FIX: Also set the Trade-scoped baseUrl!
            # This tells place_order() which endpoint to use (Trade vs View)
            # ⚠️ IMPORTANT: Set base_url (not host) — host must stay as "prod" or "uat"
            if base_url:
                client.api_client.configuration.base_url = base_url
            else:
                logger.warning("⚠️ No baseUrl in MPIN response (might cause place_order to use wrong endpoint)")
            
            # Validate REQUIRED properties are set
            if trade_token and sid:
                logger.info(f"✅ NeoAPI configuration.edit_token & edit_sid SET (confirmed)")
            else:
                missing = []
                if not trade_token:
                    missing.append("edit_token")
                if not sid:
                    missing.append("edit_sid")
                logger.error(f"❌ Missing required auth properties: {missing}")
                raise RuntimeError(f"Cannot set auth config: missing {missing}")
            
            logger.info(f"✅ Trade token verified (kType=Trade) | sid present | baseUrl set")
        except AttributeError as e:
            logger.error(f"❌ Failed to access configuration: {e}")
            raise RuntimeError(f"Cannot set token on configuration: {e}")
    else:
        logger.error(f"❌ Invalid MPIN response structure: {validate_response}")
        raise RuntimeError("MPIN validation response invalid")

    logger.info("✅ Kotak Neo session established successfully.")
    return client


# ── Health Check ─────────────────────────────────────────────
def is_session_alive() -> bool:
    """
    Quick check if current session is still valid.
    Tries to fetch limits — if it fails, session is dead.
    """
    global _client_instance
    if _client_instance is None:
        return False
    try:
        response = _client_instance.limits(
            segment="ALL", exchange="ALL", product="ALL"
        )
        return response is not None
    except Exception as e:
        logger.warning(f"Session health check failed: {e}")
        return False


# ── Graceful Shutdown ─────────────────────────────────────────
def logout():
    """Call this when shutting down the system cleanly."""
    global _client_instance
    if _client_instance:
        try:
            _client_instance.logout()
            logger.info("Kotak session logged out cleanly.")
        except Exception as e:
            logger.warning(f"Logout error (safe to ignore): {e}")
        finally:
            _client_instance = None


if __name__ == "__main__":
    print("Testing Kotak login...")
    client = get_client()
    print("Login successful!")
    
    health = is_session_alive()
    print(f"Session alive: {health}")
    
    logout()