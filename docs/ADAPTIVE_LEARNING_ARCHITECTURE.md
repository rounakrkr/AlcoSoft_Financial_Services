# AlcoSoft Adaptive Learning Architecture
**Date**: 2026-05-28  
**Status**: Phase 1 Complete (Foundation)

---

## 🎯 Core Concept

**Old**: AI predicts trades (War Room — now deprecated)  
**New**: AI continuously estimates reliability → Strategy adapts automatically

```
Trade Outcomes
    ↓
Reflection Measures Performance
    ↓
Statistical Analysis
    ↓
Adaptive Parameters Updated
    ↓
strategy.py Reads Changes
    ↓
Trading Behavior Evolves Automatically
```

---

## 📦 Components Implemented

### 1. **reflection/reflection_statistics.py** ✅
Core statistics tracking database and functions.

**Tracks**:
- Signal win rates (per signal type)
- Time-window performance (9:15-10:00, etc.)
- Per-symbol behavior (drawdown, volatility, recovery)
- Market observations
- Adaptive config history

**Database Tables**:
- `signal_performance` — individual trade outcomes
- `time_window_performance` — grouped by market hours
- `symbol_behavior` — stock-specific metrics
- `market_observations` — continuous market snapshots
- `adaptive_config_history` — config change audit trail

**Key Functions**:
```python
# Record trade outcomes
record_signal_trade(signal_name, entry_price, exit_price, profit_loss, ...)

# Retrieve aggregated statistics
get_signal_win_rates()          # Win rates per signal
get_time_window_analysis()      # Performance by time
get_symbol_analysis()           # Per-stock metrics
get_latest_market_observation() # Current market state
```

---

### 2. **reflection/observation_loop.py** ✅
Continuous market awareness system.

**Purpose**: NOT trading decisions — only market observation

**Runs every 15 minutes**:
1. Fetch market snapshot (prices, volumes, trends)
2. Detect market condition (BULLISH/BEARISH/MIXED/RANGING)
3. Measure trend strength (STRONG/MODERATE/WEAK)
4. Estimate volatility regime (HIGH/NORMAL/LOW)
5. Analyze breakout/reversal frequency
6. Store observations with signal performance context

**Outputs**:
- Structured JSON observations logged to `data/observations/YYYY-MM-DD.jsonl`
- Database records in `market_observations` table
- Real-time market condition awareness

**Example Observation**:
```json
{
  "timestamp": "2026-05-28T14:30:00",
  "market_condition": "BULLISH",
  "trend_strength": "STRONG",
  "volatility_regime": "NORMAL",
  "breakout_frequency": "HIGH",
  "reversal_frequency": "LOW",
  "details": {
    "symbols_active": 8,
    "volume_percentile": 115,
    "signal_performance": { ... },
    "time_window_stats": { ... },
    "symbol_behavior": { ... }
  }
}
```

---

### 3. **reflection/adaptive_config.py** ✅
Generates adaptive configuration values automatically.

**Calculates**:

#### Signal Confidence Multipliers
```python
# Per signal type (e.g., "Hammer Reversal")
if win_rate >= 60%:   → 1.2  (boost confidence)
if 45% <= win_rate < 60%: → 1.0  (normal)
if win_rate < 45%:    → 0.6  (reduce confidence)
```

#### Time Window Multipliers
```python
# Per hour (e.g., "9:15-10:00")
if win_rate >= 55% and low_fakeouts: → 1.0
if 45% <= win_rate < 55%: → 0.7
if win_rate < 45%: → 0.4
```

#### Adaptive Stop Loss (Per Symbol)
```python
recommended_sl = avg_drawdown × 1.2  (20% safety buffer)
capped: 0.3% - 2.0%

Example:
NTPC average drawdown: 0.6% → SL: 0.72%
BAJFINANCE average drawdown: 1.4% → SL: 1.68%
```

#### Market Regime Multiplier
```python
BULLISH + STRONG trend → 1.1  (increase confidence)
BEARISH → 0.8  (reduce confidence)
MIXED/RANGING → 0.85
```

#### Volatility-Based Filters
```python
Low volatility stocks → 1.1 (higher confidence)
High volatility stocks → 0.7 (lower confidence)
```

**Output**: `config/trading_settings.json` with `adaptive` section

```json
{
  "adaptive": {
    "strategy": {
      "signal_confidence_multipliers": {
        "hammer_reversal": 1.2,
        "ema_crossover": 0.8,
        "bullish_engulfing": 1.0
      },
      "market_regime_multiplier": 1.05
    },
    "time_windows": {
      "9:15-10:00": 1.0,
      "11:30-1:00": 0.4,
      "2:00-3:15": 0.8
    },
    "symbol_stops": {
      "NTPC": 0.72,
      "BAJFINANCE": 1.68
    },
    "volatility_filters": {
      "NTPC": 1.1,
      "BAJFINANCE": 0.7
    },
    "generated_at": "2026-05-28T14:30:00",
    "notes": "Auto-generated adaptive config"
  }
}
```

---

## 🔄 The Learning Loop (In Progress)

### Phase 1: Foundation ✅ COMPLETE
- ✅ Reflection statistics database
- ✅ Signal tracking (win rates, drawdowns, RR)
- ✅ Time window analysis
- ✅ Per-symbol behavior analysis
- ✅ Continuous market observations
- ✅ Adaptive config generation

### Phase 2: Integration (NEXT)
- Update `strategy.py` to read adaptive parameters
- Hook `observation_loop.py` into main.py scheduler
- Create trade outcome recording when positions close
- Enable signal reliability tracking

### Phase 3: Autonomous Evolution (FUTURE)
- Auto-trigger config updates based on signal degradation
- Drift detection (when signal win rate drops suddenly)
- Seasonal pattern recognition
- Multi-day correlation analysis

---

## 📊 Data Flow Example

### Day 1 — Trade Execution
```
9:30 AM: Bullish Engulfing + Volume Breakout signal fires
          → strategy.py places BUY at ₹100
          → SL at ₹99.3

10:45 AM: Position closes at ₹102
         → Profit: ₹200 (2 RR achieved)
         → Drawdown encountered: 0.8%
```

### Reflection System Tracks
```
record_signal_trade(
  signal_name="Bullish Engulfing + Volume Breakout",
  entry_price=100,
  exit_price=102,
  profit_loss=200,
  win=True,
  drawdown=0.8,
  confidence=75
)
```

### End of Day — Adaptive Config Update
```
observation_loop runs every 15 mins
  → Collects all signal outcomes
  → Calculates current win rates:
     - Bullish Engulfing: 5 wins / 8 trades = 62.5% ✅
     - EMA Crossover: 2 wins / 7 trades = 28.6% ❌
     - Volume Breakout: 4 wins / 6 trades = 66.7% ✅

adaptive_config.py generates:
  - signal_confidence_multipliers:
      bullish_engulfing: 1.2 (boost, was 1.0)
      ema_crossover: 0.5 (reduce, was 1.0)
      volume_breakout: 1.2 (boost)
  
  - symbol_stops:
      RELIANCE: 0.72% (based on 0.6% avg drawdown)
```

### Next Trading Day
```
strategy.py reads adaptive config:
  → Bullish Engulfing now trusted 120% more
  → EMA Crossover confidence reduced by 50%
  → RELIANCE gets tighter stop loss

Result: Trading behavior adapts automatically
        No manual tuning needed
```

---

## 🎯 Key Design Principles

### ✅ DO

- Keep architecture lightweight
- Keep systems modular
- Prioritize reliability
- Prioritize measurable statistics
- Use structured outputs
- Maintain deterministic execution
- Build for maintainability

### ❌ DO NOT

- Rebuild War Room
- Create debate systems
- Create mediator agents
- Create complex orchestration
- Create autonomous AI traders
- Let AI predict trades directly

---

## 🔧 Usage

### Record a Completed Trade
```python
from reflection.reflection_statistics import record_signal_trade

record_signal_trade(
    signal_name="Hammer Reversal",
    entry_price=499.5,
    exit_price=502.0,
    profit_loss=250,
    win=True,
    drawdown=1.2,
    confidence=65,
    recovery_time_minutes=15,
)
```

### Run Observation Cycle (Manual)
```python
from reflection.observation_loop import run_observation_cycle
import asyncio

asyncio.run(run_observation_cycle())
```

### Generate Adaptive Config (Manual)
```python
from reflection.adaptive_config import apply_adaptive_config

apply_adaptive_config(dry_run=True)   # Preview changes
apply_adaptive_config(dry_run=False)  # Apply changes
```

### View Adaptive Config Summary
```python
from reflection.adaptive_config import print_adaptive_config_summary

print_adaptive_config_summary()
```

---

## 📁 File Structure

```
reflection/
├── reflection_statistics.py   (database + tracking functions)
├── observation_loop.py        (continuous market observations)
├── adaptive_config.py         (config generation)
└── reflection_loop.py         (existing — trade outcome analysis)

data/
├── reflection_statistics.db   (SQLite database)
├── observations/              (JSONL observation logs)
└── live_capital.json

config/
└── trading_settings.json      (includes "adaptive" section)
```

---

## ⚙️ Next Steps

1. **Integrate observation loop into main.py scheduler**
   - Add scheduled job: every 15 minutes during market hours
   
2. **Hook signal tracking into order_executor.py**
   - Record outcomes when positions close
   - Track drawdowns in real-time
   
3. **Update strategy.py to read adaptive parameters**
   - Read signal confidence multipliers
   - Apply time window multipliers
   - Use symbol-specific SL values
   
4. **Enable auto-config updates**
   - Run adaptive_config every 6 hours
   - Store history for audit trail

---

## 📈 Expected Benefits

- **Signals auto-improve** based on measured performance
- **Weak signals automatically reduced** without manual coding
- **Time windows adapted** to market conditions
- **Symbol-specific tuning** based on real behavior
- **Fully auditable** — all changes logged and measurable
- **Zero manual intervention** — system evolves automatically

This is the foundation for true adaptive learning.

Not AI predicting trades.

**AI estimating reliability**.

---

**Created**: 2026-05-28 | **Updated**: [auto]  
**Status**: Foundation Complete | **Next Phase**: Integration
