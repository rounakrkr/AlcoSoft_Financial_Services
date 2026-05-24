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
    except Exception:
        pass  # Session may already be dead, ignore

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

    logger.info("Initializing Kotak Neo session...")

    # Step 1 — Init client
    client = NeoAPI(
        environment="prod",
        access_token=None,
        neo_fin_key=None,
        consumer_key=consumer_key,
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

    # Small buffer — let Kotak process the login
    time.sleep(2)

    # Step 4 — Validate with MPIN (gets trade token — needed for orders)
    validate_response = client.totp_validate(mpin=mpin)
    logger.info(f"MPIN validation response: {validate_response}")

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