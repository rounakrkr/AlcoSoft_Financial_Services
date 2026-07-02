"""Tests for the capital + WebSocket hardening (Fixes A/C/D)."""
import sys
import types


# ── Fix C: negative/zero Net is a VALID free-cash reading, not a failure ──────

def test_fixC_negative_net_is_valid(monkeypatch):
    fake = types.ModuleType("core.kotak_client")

    class FC:
        def limits(self, **k):
            return {"Net": "-131.47", "availablecash": None, "data": None}
    fake.get_client = lambda: FC()
    monkeypatch.setitem(sys.modules, "core.kotak_client", fake)

    import core.order_executor as oe
    monkeypatch.setattr(oe, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(oe, "_capital_fetch_window_open", lambda: True)
    oe._capital_api_failures = 3
    oe._capital_last_fetch_ok = False
    oe._capital_last_update = 0.0

    val = oe._get_available_capital(force_refresh=True)
    assert val == 0.0, val                      # max(0, -131.47)
    assert oe._capital_api_failures == 0        # NOT a failure
    assert oe.is_capital_fresh() is True         # valid reading, even though value 0
    print("Fix C OK: Net=-131.47 -> available=0, failures=0, fresh=True")


def test_fixC_broken_payload_counts_as_failure(monkeypatch):
    fake = types.ModuleType("core.kotak_client")

    class FC:
        def limits(self, **k):
            return {"Net": None, "availablecash": None, "data": None}  # unusable
    fake.get_client = lambda: FC()
    monkeypatch.setitem(sys.modules, "core.kotak_client", fake)

    import core.order_executor as oe
    monkeypatch.setattr(oe, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(oe, "_capital_fetch_window_open", lambda: True)
    oe._capital_api_failures = 0
    oe._capital_last_fetch_ok = True
    oe._capital_cache = 999.0
    oe._capital_last_update = 1.0

    val = oe._get_available_capital(force_refresh=True)
    assert val == 999.0, val                    # serves stale cache
    assert oe._capital_api_failures == 1        # genuine failure counted
    assert oe.is_capital_fresh() is False
    print("Fix C OK: empty payload -> failure counted, serves cache, not fresh")


# ── Fix D: stale-client callback guard (epoch) ────────────────────────────────

def test_fixD_stale_epoch_close_ignored(monkeypatch):
    import core.data_fetcher as df
    df._client_epoch = 5
    df._subscribed_symbols = ["X"]
    called = {"sched": 0}
    monkeypatch.setattr(df, "_schedule_reconnect", lambda: called.__setitem__("sched", called["sched"] + 1))
    monkeypatch.setattr(df, "_schedule_reconnect_after_market_open", lambda: called.__setitem__("sched", called["sched"] + 1))
    monkeypatch.setattr(df, "_is_market_open", lambda: True)

    df._on_close("bye", epoch=3)                # stale
    assert called["sched"] == 0, "stale close must NOT schedule a reconnect"

    df._on_close("bye", epoch=5)                # current
    assert called["sched"] == 1, "current-epoch close should schedule exactly one reconnect"
    print("Fix D OK: stale-epoch close ignored, current-epoch close scheduled once")


# ── Fix D: force_reconnect dedup/throttle ─────────────────────────────────────

def test_fixD_force_reconnect_throttled(monkeypatch):
    import core.kotak_client as kc

    logins = {"n": 0}

    def fake_get_client():
        logins["n"] += 1
        return object()
    monkeypatch.setattr(kc, "get_client", fake_get_client)
    # neutralize the feed restart side-effect
    fake_df = types.ModuleType("core.data_fetcher")
    fake_df.restart_live_feed = lambda: None
    monkeypatch.setitem(sys.modules, "core.data_fetcher", fake_df)

    kc._client_instance = None
    kc._reconnect_in_progress = False
    kc._last_reconnect_ts = 0.0

    kc.force_reconnect()          # real reconnect
    first = logins["n"]
    kc.force_reconnect()          # within 30s -> throttled, no new login
    kc.force_reconnect()          # throttled
    assert logins["n"] == first, f"throttle failed: {logins['n']} logins (expected {first})"
    print(f"Fix D OK: force_reconnect throttled — {logins['n']} login after 3 rapid calls")
