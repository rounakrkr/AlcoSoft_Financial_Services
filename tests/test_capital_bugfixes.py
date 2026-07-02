"""Logic tests for the two live-engine capital bugs (broker/DB mocked)."""
import core.order_executor as oe


def _fake_cfg(section, key, default=None):
    vals = {("risk", "margin_leverage"): 5.0, ("risk", "allow_margin"): True,
            ("risk", "paper_capital"): 10000, ("risk", "max_daily_loss_percent"): 0.05}
    return vals.get((section, key), default)


def test_bug1_window_gate_blocks_offmarket_fetch(monkeypatch):
    """Off-market: serve cache, never hit broker, never increment failure counter."""
    monkeypatch.setattr(oe, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(oe, "_capital_fetch_window_open", lambda: False)
    oe._capital_cache = 12345.0
    oe._capital_last_update = 0.0
    oe._capital_api_failures = 0

    # If the gate works, the broker import/fetch block is never reached, so we
    # simply assert the cache is served and no failure is counted.
    val = oe._get_available_capital(force_refresh=True)
    assert val == 12345.0, val
    assert oe._capital_api_failures == 0, oe._capital_api_failures
    print("Bug1 gate OK: returned cache=%.2f, failures=%d" % (val, oe._capital_api_failures))


def test_bug2_starting_capital_reconstructed_when_position_open(monkeypatch):
    """LIVE, capital_start NULL, position open -> reconstruct ~full start capital."""
    monkeypatch.setattr(oe, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(oe, "cfg", _fake_cfg)
    monkeypatch.setattr(oe, "_get_available_capital", lambda force_refresh=False: 34.62)
    monkeypatch.setattr(oe, "get_today_gross_pnl", lambda: 0.0)
    monkeypatch.setattr(oe, "_current_position_valuation", lambda: {
        "entry_position_value": 100128.0,
        "current_position_value": 100128.0,
        "unrealized_pnl": 0.0,
    })
    import core.state_manager as sm
    monkeypatch.setattr(sm, "get_today_stats", lambda: {"capital_start": None})

    snap = oe.get_margin_status()
    sc = snap["starting_capital"]
    # 34.62 + 100128/5 - 0 = 20060.22
    assert 20000 <= sc <= 20120, sc
    assert snap["account_equity"] >= 20000, snap["account_equity"]
    print("Bug2 reconstruct OK: starting_capital=%.2f (was collapsing to 34.62)" % sc)


def test_bug2_persisted_capital_start_is_respected(monkeypatch):
    """When capital_start IS persisted, it must be used verbatim (no reconstruction)."""
    monkeypatch.setattr(oe, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(oe, "cfg", _fake_cfg)
    monkeypatch.setattr(oe, "get_today_gross_pnl", lambda: 0.0)
    monkeypatch.setattr(oe, "_current_position_valuation", lambda: {
        "entry_position_value": 100128.0, "current_position_value": 100128.0, "unrealized_pnl": 0.0,
    })
    import core.state_manager as sm
    monkeypatch.setattr(sm, "get_today_stats", lambda: {"capital_start": 20060.0})
    snap = oe.get_margin_status()
    assert abs(snap["starting_capital"] - 20060.0) < 0.01, snap["starting_capital"]
    print("Bug2 persisted OK: starting_capital=%.2f" % snap["starting_capital"])
