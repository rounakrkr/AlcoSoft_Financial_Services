# Reflection Statistics Collection System

## Overview

The Reflection Statistics Collection System is the **machine learning foundation** for AlcoSoft's adaptive trading behavior. It's a deterministic, statistics-based system that:

- Tracks signal performance across trading sessions
- Measures time-window effectiveness
- Analyzes per-symbol behavior and volatility
- Powers automatic parameter tuning without AI agents

**This is NOT a daily journal.** This is **persistent machine memory** that accumulates and learns from historical outcomes.

---

## Architecture

### Three Core Modules

#### 1. `reflection_engine.py` — Statistical Memory
**Purpose:** Store and retrieve performance metrics
**Location:** `reflection/reflection_engine.py`

**Key Responsibilities:**
- Record completed trades with outcome data
- Maintain SQLite database of signal performance
- Calculate aggregated statistics (win rate, average profit, etc.)
- Provide reliable metrics for adaptive config generation

**Key Functions:**
```python
record_trade(
    signal_name: str,
    symbol: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    confidence: float,
    time_window: str,
    drawdown: float = 0.0,
    sl_hit: bool = False,
    recovered: bool = False,
) -> bool
```

Records a completed trade and automatically updates derived statistics.

**Database Schema:**
- `signal_performance` — Per-signal metrics (total trades, wins, losses, win rate, avg profit, avg RR)
- `time_window_performance` — Per-hour-window metrics (trade count, win rate, avg PnL)
- `symbol_behavior` — Per-stock metrics (SL hit freq, recovery probability, volatility profile)
- `trade_records` — Historical log of all trades for analysis

#### 2. `adaptive_config_updater.py` — Config Generator
**Purpose:** Calculate adaptive multipliers and update trading_settings.json
**Location:** `reflection/adaptive_config_updater.py`

**Key Function:**
```python
apply_adaptive_config() -> bool
```

Reads all reflection statistics and generates updated trading_settings.json with:
```json
{
  "adaptive": {
    "strategy": {
      "signal_confidence_multipliers": {
        "hammer": 1.1,
        "rsi_macd": 0.8,
        "bollinger_bounce": 0.95
      },
      "market_regime_multiplier": 1.05
    },
    "time_windows": {
      "9:15-10:00": 0.9,
      "10:00-11:30": 1.0,
      "11:30-1:00": 0.75,
      "2:00-3:30": 0.85
    },
    "symbol_stops": {
      "NTPC": 0.8,
      "RELIANCE": 1.1,
      "INFY": 0.9
    }
  }
}
```

---

## Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│  Trade Execution (core/strategy.py)                        │
│  Places buy order with signal name, confidence, symbol    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Order Execution (core/order_executor.py)                  │
│  - Places order on broker                                   │
│  - Tracks entry price, exit price, P&L                      │
│  - Calls record_trade() when position closes               │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  reflection_engine.record_trade()                           │
│  - Stores trade in trade_records table                     │
│  - Updates signal_performance (win rate, avg profit, etc.) │
│  - Updates time_window_performance                         │
│  - Updates symbol_behavior (volatility, recovery prob)    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Reflection Loop (reflection/reflection_loop.py)           │
│  - Runs daily at 3:35 PM                                   │
│  - Owl Alpha journals/reflects on day's trades             │
│  - Calls apply_adaptive_config()                           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  adaptive_config_updater.apply_adaptive_config()           │
│  - Reads all signal statistics from reflection_engine     │
│  - Calculates confidence multipliers (0.4 to 1.2 range)   │
│  - Calculates time window multipliers                      │
│  - Calculates symbol-specific SL adjustments               │
│  - Writes updated trading_settings.json                    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Next Trading Day (core/strategy.py)                        │
│  - Loads adaptive config from trading_settings.json        │
│  - Applies signal multipliers to confidence scores         │
│  - Applies time window multipliers                         │
│  - Uses symbol-specific stop losses                        │
│  - Behavior evolved from previous day's outcomes           │
└─────────────────────────────────────────────────────────────┘
```

---

## Multiplier Calculation Rules

### Signal Confidence Multipliers
```
Win Rate < 40%:  multiplier = 0.4  (suppress)
Win Rate 40-50%: multiplier = 0.6-0.8 (reduce)
Win Rate 50-60%: multiplier = 0.8-0.9 (neutral-slight)
Win Rate 60-70%: multiplier = 0.9-1.1 (boost)
Win Rate > 70%:  multiplier = 1.2  (strong boost)

Requires minimum 10 trades for reliability
```

### Time Window Multipliers
```
Same scale as signal multipliers
Applied to all signals in that hourly window
Identifies best/worst trading hours
Example: "9:15-10:00" consistently weak → 0.6 multiplier

Requires minimum 20 trades for reliability
```

### Symbol Stop Loss Adjustments
```
High Volatility (avg drawdown > 3%):   1.2x (wider SL)
Normal Volatility (1.5-3%):             1.0x (normal)
Low Volatility (< 1.5%):                0.8x (tighter SL)

Applied to base stop loss percentage from strategy.py
```

### Market Regime Multiplier
```
Overall Win Rate < 45%:  0.8  (reduce confidence)
Overall Win Rate 45-50%: 0.9  (slight reduce)
Overall Win Rate 50-55%: 1.0  (neutral)
Overall Win Rate 55-60%: 1.05 (slight boost)
Overall Win Rate > 60%:  1.1  (boost confidence)

Applied to ALL signals as market-wide adjustment
```

---

## Integration Points

### 1. Order Executor (`core/order_executor.py`)
**When:** Position closes (sell order executed)
**Action:** Calls `record_trade()` with complete trade data

```python
record_trade(
    signal_name=strategy_context,  # e.g., "Hammer" or "RSI+MACD Momentum"
    symbol=symbol,                  # e.g., "NTPC"
    entry_price=entry,              # ₹365.50
    exit_price=exit_price,          # ₹368.20
    pnl=pnl,                        # ₹2.70 (profit)
    confidence=confidence,          # 85 (confidence when entered)
    time_window=time_window,        # "9:15-10:00"
    drawdown=drawdown,              # 0.5% (max drawdown during hold)
    sl_hit=False,                   # Was SL hit?
    recovered=False,                # Did recover after SL?
)
```

### 2. Reflection Loop (`reflection/reflection_loop.py`)
**When:** Daily at 3:35 PM
**Action:** Calls `apply_adaptive_config()` to update trading_settings.json

```python
from reflection.adaptive_config_updater import apply_adaptive_config

# ... Owl Alpha reflection logic ...

try:
    logger.info("🔧 Updating adaptive configuration based on reflection...")
    apply_adaptive_config()
    logger.info("✅ Adaptive configuration updated successfully")
except Exception as e:
    logger.warning(f"Adaptive config update failed (non-critical): {e}")
```

### 3. Strategy Engine (`core/strategy.py`)
**When:** Trade evaluation (every market tick)
**Action:** Reads adaptive config and applies multipliers

```python
# Load adaptive config at startup
_load_adaptive_config()

# In _evaluate_buy_signal():
adaptive_confidence = base_confidence

# Apply signal confidence multipliers
for signal in fired:
    signal_key = signal["name"].lower().replace(" ", "_")
    if signal_key in _adaptive_signal_multipliers:
        mult = _adaptive_signal_multipliers[signal_key]
        adaptive_confidence *= mult

# Apply time window multiplier
time_window = _get_time_window(datetime.now().time())
if time_window in _adaptive_time_multipliers:
    adaptive_confidence *= _adaptive_time_multipliers[time_window]

# Apply market regime multiplier
adaptive_confidence *= _adaptive_market_multiplier

# Cap between 0-100
adaptive_confidence = max(0, min(100, adaptive_confidence))
```

---

## Usage Examples

### Recording a Trade
```python
from reflection.reflection_engine import record_trade

# After position closes in order_executor.py
record_trade(
    signal_name="Hammer",
    symbol="NTPC",
    entry_price=365.50,
    exit_price=368.20,
    pnl=2.70,
    confidence=85,
    time_window="9:15-10:00",
    drawdown=0.5,
    sl_hit=False,
    recovered=False,
)
```

### Querying Performance Metrics
```python
from reflection.reflection_engine import (
    get_signal_stats,
    get_all_signal_stats,
    get_time_window_stats,
    get_symbol_stats,
)

# Get specific signal performance
hammer_stats = get_signal_stats("Hammer")
print(f"Hammer: {hammer_stats['winning_trades']}/{hammer_stats['total_trades']} wins")

# Get all signals
all_signals = get_all_signal_stats()
for sig in all_signals:
    print(f"{sig['signal_name']}: WR={sig['win_rate']:.1f}%")

# Get time window performance
morning_stats = get_time_window_stats("9:15-10:00")
print(f"Morning: {morning_stats['win_rate']:.1f}% win rate")

# Get symbol volatility
ntpc_stats = get_symbol_stats("NTPC")
print(f"NTPC volatility: {ntpc_stats['volatility_profile']}")
```

### Generating Adaptive Config
```python
from reflection.adaptive_config_updater import (
    apply_adaptive_config,
    get_adaptive_config_summary,
)

# Calculate and apply updated config
apply_adaptive_config()

# Check what was updated
summary = get_adaptive_config_summary()
print(f"Config updated for {summary['signal_count']} signals")
```

### Saving Snapshots
```python
from reflection.reflection_engine import save_reflection_snapshot

# Backup current statistics as JSON
filename = save_reflection_snapshot()
# Saved to: data/reflection_snapshots/reflection_20260529_143500.json
```

---

## Database Snapshots

Every time `save_reflection_snapshot()` is called, a JSON backup is created:

```json
{
  "timestamp": "2026-05-29T14:35:00.123456",
  "signals": [
    {
      "signal_name": "Hammer",
      "total_trades": 47,
      "winning_trades": 31,
      "losing_trades": 16,
      "win_rate": 65.96,
      "avg_profit": 2.45,
      "avg_loss": 1.82,
      "avg_rr": 1.35,
      "avg_drawdown": 0.72
    },
    ...
  ],
  "time_windows": [
    {
      "time_window": "9:15-10:00",
      "trade_count": 12,
      "win_rate": 58.33,
      "avg_pnl": 1.50,
      "failure_rate": 41.67
    },
    ...
  ],
  "symbols": [
    {
      "symbol": "NTPC",
      "sl_hit_freq": 15.4,
      "recovery_prob": 45.0,
      "avg_drawdown": 0.95,
      "volatility_profile": "NORMAL"
    },
    ...
  ]
}
```

---

## Key Design Principles

### 1. **Deterministic & Measurable**
- No AI agents generating signals
- Only statistics-based multipliers
- Every number can be traced to trade outcomes
- Reproducible results

### 2. **Persistent Memory**
- Data accumulates across sessions
- Long-term patterns emerge
- Minimum trade counts before adjustment
- No single-day overreaction

### 3. **Safety Bounds**
- Multipliers capped at 0.4 to 1.2 range
- Requires minimum trade samples (10-20)
- Non-critical failures don't crash system
- Config updates are additive (doesn't remove old signals)

### 4. **Automatic Tuning**
- No manual config updates needed
- Runs daily at end of market
- Parameters adjust based on real outcomes
- System learns and improves over time

---

## Troubleshooting

### No Statistics Generated
**Check:**
1. `record_trade()` is being called from order_executor.py
2. Trades are being recorded (check `data/reflection.db`)
3. Time windows match the values in strategy.py

**Debug:**
```python
from reflection.reflection_engine import get_database_summary
print(get_database_summary())
```

### Multipliers Not Applied
**Check:**
1. `_load_adaptive_config()` is called on strategy.py startup
2. trading_settings.json has "adaptive" section
3. Signal names are lowercased with underscores (e.g., "rsi_macd")

**Debug:**
```python
# In strategy.py
print(f"Loaded multipliers: {_adaptive_signal_multipliers}")
print(f"Loaded windows: {_adaptive_time_multipliers}")
```

### Config Not Updating
**Check:**
1. reflection_loop runs at 3:35 PM
2. `apply_adaptive_config()` is called
3. trading_settings.json is writable
4. SQLite database has trade records

**Debug:**
```python
from reflection.adaptive_config_updater import get_adaptive_config_summary
print(get_adaptive_config_summary())
```

---

## Future Enhancements

1. **Volume Weighting** - Weight recent trades more heavily
2. **Correlation Analysis** - Detect signal interactions
3. **Market Regime Detection** - Adjust based on market condition
4. **Volatility Weighting** - Scale multipliers by recent volatility
5. **Performance Decay** - Discount older trades gradually
6. **A/B Testing** - Compare two config versions side-by-side

---

## Related Documentation

- [Adaptive Learning Architecture](ADAPTIVE_LEARNING_ARCHITECTURE.md) - High-level system design
- [Phase 1 Completion Summary](PHASE1_COMPLETION_SUMMARY.md) - Original implementation details
- [Strategy Documentation](../core/strategy.py) - Signal generation logic
