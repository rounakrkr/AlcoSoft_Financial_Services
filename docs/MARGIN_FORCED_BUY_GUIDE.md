# 🔥 Margin & Forced Buy System (2026-05-28)

## Problem Solved

**Before**: Just a flag. If you had ₹800 and stock cost ₹1000, margin alone didn't help you buy more.

**After**: Intelligent margin deployment with forced buy capability. Now you can:
- Buy with full margin leverage multiplier
- Force buying even when risk calc says qty=0
- Track margin deployment across positions
- Pyramid positions with tranche buying

---

## How It Works

### Example Scenario
```
Real Capital: ₹800
Stock Price: ₹1000
Margin Leverage: 2.0x (2:1)
```

**Without margin**:
- Available: ₹800
- Affordable: 0 shares → BUY REJECTED ❌

**With margin (old flag-only approach)**:
- Available: ₹800 × 2 = ₹1600
- Affordable: 1 share
- Result: Buys only 1 share 😞

**With forced_buy_margin (new approach)**:
- Available: ₹800 × 2 = ₹1600
- Position size %: 100% (forced mode)
- Affordable: 1 share (limited by price)
- Result: Buys 1 share at max power
- **Key**: Can now use `calculate_quantity_with_tranches()` to buy multiple positions later

---

## Configuration

### Settings in `config/trading_settings.json`

```json
{
  "risk": {
    "allow_margin": true,
    "forced_buy_margin": false,
    "margin_leverage": 2.0,
    "position_size_margin": 0.75
  }
}
```

| Setting | Type | Default | What It Does |
|---------|------|---------|-------------|
| `allow_margin` | bool | `false` | Enable margin trading at all |
| `forced_buy_margin` | bool | `false` | 🔥 **NEW**: Force buy with 100% margin even if risk calc says 0 |
| `margin_leverage` | float | `2.0` | 2.0 = 2x leverage (₹1000 → ₹2000 available) |
| `position_size_margin` | float | `0.75` | 75% of margin capital per position (unless forced buy) |

---

## Three Usage Modes

### Mode 1: Conservative (Default)
```python
"allow_margin": False,
"forced_buy_margin": False
```
- Uses ONLY real capital
- Rejects orders if insufficient funds
- Best for: Learning, low risk tolerance

### Mode 2: Margin Enabled (Moderate)
```python
"allow_margin": True,
"forced_buy_margin": False,
"position_size_margin": 0.75
```
- Multiplies capital: real × margin_leverage
- Uses 75% of available per position
- Example: ₹1000 real × 2x = ₹2000 available → max ₹1500 per position
- Rejects if can't afford even with margin

### Mode 3: Forced Buy (Aggressive) 🔥
```python
"allow_margin": True,
"forced_buy_margin": True,
"position_size_margin": 0.75  # Will be overridden to 100%
```
- Multiplies capital: real × margin_leverage
- **Ignores risk calculation, buys maximum possible**
- Example: ₹800 real × 2x = ₹1600 available → buys 1 share @ ₹1000
- **Warning**: Can over-leverage if not careful

---

## Key Functions

### 1. `calculate_quantity(price, stop_loss, risk_pct)`
Standard single-order quantity calculation. Now supports forced_buy.

```python
from core.order_executor import calculate_quantity

qty = calculate_quantity(price=1000, stop_loss=980)
# Returns: int (number of shares to buy)
```

**Flow with forced_buy_margin**:
1. Calculate quantity based on risk tolerance
2. If qty=0 AND forced_buy_margin=true: buy maximum affordable
3. Otherwise: return qty (could still be 0 if even max unaffordable)

---

### 2. `calculate_quantity_with_tranches()` 🚀 **NEW**
Break quantity into multiple pyramid tranches.

```python
from core.order_executor import calculate_quantity_with_tranches

result = calculate_quantity_with_tranches(
    price=1000,
    stop_loss=980,
    max_tranches=3  # Up to 3 buys
)

print(result)
# {
#     'total_qty': 3,
#     'num_tranches': 3,
#     'per_tranche_qty': 1,
#     'margin_used': 2200.0,
#     'margin_ratio': 2.75
# }
```

**Use case**: Buy 1 share now, then 1 more on 2% dip, then 1 more on 4% dip (pyramid strategy).

---

### 3. `get_margin_status()` 📊 **NEW**
Monitor current margin deployment.

```python
from core.order_executor import get_margin_status

status = get_margin_status()
print(status)
# {
#     'real_capital': 10000.0,
#     'margin_leverage': 2.0,
#     'total_available_with_margin': 20000.0,
#     'deployed_in_positions': 15000.0,
#     'margin_used': 5000.0,
#     'margin_pct': 50.0,  # Using 50% of real capital as margin
#     'remaining_margin': 5000.0,
#     'is_over_leveraged': False
# }
```

**Use case**: Dashboard widget to show how much margin is deployed.

---

## Examples

### Example 1: Normal Mode (No Margin)
```python
# Settings: allow_margin=false
calculate_quantity(price=1000, stop_loss=980)
# ₹10,000 capital, 2% risk per trade
# max_loss = ₹10,000 × 0.02 = ₹200
# stop_distance = ₹1000 - ₹980 = ₹20
# ideal_qty = ₹200 / ₹20 = 10
# affordable_qty = (₹10,000 × 0.20) / ₹1000 = 2
# Result: min(10, 2) = 2 shares ✓
```

### Example 2: Margin Mode (Moderate)
```python
# Settings: allow_margin=true, margin_leverage=2.0, position_size_margin=0.75
calculate_quantity(price=1000, stop_loss=980)
# ₹10,000 capital, 2% risk per trade
# margin available = ₹10,000 × 2 = ₹20,000
# max_loss = ₹10,000 × 0.02 = ₹200 (still based on real capital!)
# stop_distance = ₹1000 - ₹980 = ₹20
# ideal_qty = ₹200 / ₹20 = 10
# affordable_qty = (₹20,000 × 0.75) / ₹1000 = 15
# Result: min(10, 15) = 10 shares ✓
# Margin deployed: 10 × ₹1000 - ₹10,000 = ₹0 (well within real capital)
```

### Example 3: Forced Buy (Aggressive) 🔥
```python
# Settings: allow_margin=true, forced_buy_margin=true
# Same scenario but smaller real capital
calculate_quantity(price=1000, stop_loss=980)
# ₹800 real capital, 2% risk
# margin available = ₹800 × 2 = ₹1600
# max_loss = ₹800 × 0.02 = ₹16
# stop_distance = ₹20
# ideal_qty = ₹16 / ₹20 = 0 (can't afford with risk limit!)
# But forced_buy_margin=true...
# affordable_qty = (₹1600 × 1.0) / ₹1000 = 1.6 = 1 share
# Result: 1 share (forced buy bypassed risk calc!) 🔥
# Margin deployed: 1 × ₹1000 - ₹800 = ₹200 (margin ratio: 25%)
```

### Example 4: Tranche Buying 📊
```python
# Want to buy ₹1000 stock with ₹800 real capital, 2x margin
# Buy in 3 tranches (pyramid up on dips)
result = calculate_quantity_with_tranches(
    price=1000,
    stop_loss=980,
    max_tranches=3
)
# Result:
# total_qty: 1 (can only afford 1 total)
# num_tranches: 1
# per_tranche_qty: 1

# But if stock drops to ₹990:
result = calculate_quantity_with_tranches(
    price=990,
    stop_loss=970,
    max_tranches=3
)
# Might allow more tranches: 3 × 1 = 3 shares total
```

---

## Safety Features

### 1. Over-Leverage Detection
When placing a buy order, system checks:
```python
total_deployed = current_open_positions + this_new_order
if total_deployed > (real_capital × margin_leverage):
    logger.warning("⚠️ OVER-LEVERAGE WARNING")
    # Still places order but warns you
```

### 2. Risk-Based Position Sizing
Even with forced buy, position size is still limited by:
- Available capital (real + margin)
- Stock price
- Your max_risk_per_trade setting

### 3. Real Capital Always Protected
Risk calculations ALWAYS use real capital, never margin:
```python
max_loss = capital * risk_pct  # ← Uses real capital ONLY
```

---

## Dashboard Integration

The dashboard can now show:

```python
# Get margin status
status = get_margin_status()

# Display:
# 💰 Real Capital: ₹10,000
# 🔥 Margin Available: ₹10,000 (2x)
# 📊 Deployed in Positions: ₹8,000
# 🔴 Margin Used: ₹0 (0%)
# ⚪ Remaining Margin: ₹10,000
# ✅ Over-leveraged: No
```

---

## Migration Guide (If You Had Manual Margin Logic)

If you previously implemented margin manually, switch to this:

**OLD**:
```python
if enable_margin:
    capital *= 2  # Naive approach
```

**NEW**:
```python
quantity = calculate_quantity(price, stop_loss)
# Automatically respects:
# - allow_margin setting
# - margin_leverage multiplier
# - forced_buy_margin flag
# - risk-based position sizing
```

---

## Risk Management Checklist

✅ **Before enabling forced_buy_margin**:
- [ ] Test in PAPER mode first
- [ ] Set a small max_daily_loss_percent (e.g., 2-5%)
- [ ] Understand stop-loss execution times
- [ ] Have manual circuit breaker ready
- [ ] Monitor margin_pct in logs (should not exceed 80%)

⚠️ **During forced buy trading**:
- [ ] Check get_margin_status() regularly
- [ ] Ensure stops are tight (forced buy = higher leverage)
- [ ] Avoid multiple forced buys stacking up
- [ ] Monitor logs for over-leverage warnings

🛑 **If over-leveraged (is_over_leveraged=true)**:
- [ ] Don't place new orders
- [ ] Close worst-performing position
- [ ] Reduce position_size_margin to 0.5
- [ ] Or disable forced_buy_margin temporarily

---

## FAQ

**Q: Can I force buy a stock I can't normally afford?**
A: Yes! If you have ₹1000 capital + 2x margin and stock costs ₹1500, you CAN'T buy normally. But with forced_buy_margin, you could buy 1 share using ₹500 margin.

**Q: What happens if a stop loss hits hard with forced buy?**
A: Loss is still capped by max_daily_loss_percent circuit breaker. But the loss amount is larger because you bought more shares with margin.

**Q: Can I use forced_buy without margin?**
A: No. forced_buy_margin requires allow_margin=true, or it's just ignored.

**Q: How do I know if I'm over-leveraged?**
A: Check logs for "⚠️ OVER-LEVERAGE WARNING" and call get_margin_status() to see is_over_leveraged=true.

**Q: Should I always use forced_buy_margin?**
A: No. Use it only if:
- You have tight stops
- You understand the risks
- Your broker allows it
- Your daily loss limit is low enough
