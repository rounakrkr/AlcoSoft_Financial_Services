# 📋 Order Field Reference Guide

> **Last Updated**: May 28, 2026  
> **Status**: ✅ Margin Fix Confirmed Working

---

## Overview

Each order placed by AlcoSoft contains metadata about the trade. This guide explains what each field means.

---

## Core Order Fields

### 1. **Qty** (Quantity)
- **What**: Number of shares purchased
- **Calculation**: Based on (1) your risk tolerance, (2) stock price, and (3) stop loss distance
- **Example**: If price = ₹500, SL = ₹490, and your max risk = ₹1,000, then Qty = 100 shares
- **Key Point**: System refuses to order if you can't afford even 1 share (no margin forcing)
- **Formula**: `Qty = min(ideal_qty_from_risk, affordable_qty_from_capital)`

### 2. **Entry Price** 
- **What**: The exact price at which your order was placed
- **Display**: Shown as ₹X,XXX.XX
- **Real vs Paper**: 
  - PAPER mode: Uses live LTP (last traded price)
  - LIVE mode: Uses actual broker order confirmation price
- **Note**: May differ slightly from market price if order slipped during execution

### 3. **Stop Loss (SL)**
- **What**: Automatic loss limit — if stock falls below this, order AUTO-SELLS immediately
- **Purpose**: Risk management — limits downside
- **Calculation**: Entry Price × (1 - Risk %) 
  - Example: ₹2,450 × (1 - 1.5%) = ₹2,413.75
- **Setting**: Controlled by `config/trading_settings.json` → `risk.stop_loss_percent`
- **Status**: 🟡 **Active** — System places SL-M (Stop Loss Limit) order on broker immediately after BUY

### 4. **Target Price (TP) / Profit Target**
- **What**: Profit goal — you should manually sell above this price
- **Purpose**: Lock in gains when strategy works perfectly
- **Calculation**: Entry + (Risk × Risk:Reward Ratio)
  - Example: ₹2,450 + (₹37.50 × 2.5) = ₹3,143.75
- **Default RR Ratio**: 2.5:1 (for every ₹1 risked, target ₹2.50 profit)
- **Note**: Manual sell — system doesn't auto-exit at this level (SL is automatic, TP is not)

### 5. **Strategy**
- **What**: Which AI pattern triggered this buy order
- **Possible Values**:
  - `TECHNICAL` — Chart patterns (support/resistance, moving averages)
  - `FUNDAMENTAL` — Company health (P/E ratio, revenue growth)
  - `RISK` — Risk assessment agreed (all 3 strategies aligned)
- **Why it matters**: Different strategies have different success rates
- **Use case**: If one strategy is working better, filter by that strategy later

### 6. **Confidence (%)**
- **What**: How sure is the strategy about this trade (65-99%)
- **Calculation**: Voting agreement between 3+ AI strategies
  - 65% = Minimum threshold (majority agree)
  - 99% = Very strong consensus (all strategies align perfectly)
- **Your choice**: `min_confidence` in settings.json filters out weak signals
- **Example**: 78% confidence = fairly sure, but not 100% certain

### 7. **Product Type** (MIS vs CNC)
- **What**: Order type that determines MARGIN usage
  
#### **MIS** = Margin Intraday Scheme
  - ✅ Can use leverage (broker's money + your money)
  - ⚠️ **MUST close same day** (market close or squareoff time)
  - 💰 Less capital needed upfront
  - 🎯 **Current System Default**: Uses this
  - **Risk**: If market crashes, you lose MORE than invested

#### **CNC** = Cash & Carry
  - ✅ Full payment required upfront (NO margin allowed)
  - ✅ Can hold overnight/weeks/months
  - ✅ Safer (can't be forced liquidated)
  - ❌ Requires full capital tied up
  - **NOT currently used**: System uses MIS for speed

### 8. **Traded Amount** (Total Capital Used)
- **What**: Total rupees deployed on this order
- **Calculation**: `Entry Price × Quantity`
- **Example**: ₹2,450 × 5 shares = ₹12,250 deployed
- **Relevance**: How much of your available capital was used
- **Note**: Not shown in UI, but calculated internally for position sizing

---

## Status Fields (After Order Placed)

### **Order ID**
- **What**: Broker's unique reference for this order
- **PAPER mode**: Format = `PAPER-TITAN-141530` (paper-symbol-time)
- **LIVE mode**: Format = `20260528001234` (broker's reference)
- **Use**: Track order status with broker if issues occur

### **SL Order ID**  
- **What**: Separate Stop Loss order ID (auto-placed by system)
- **Why separate?**: Broker tracks SL orders independently
- **Status**: Should match main order ID if placed successfully
- **Null = Failure**: If SL failed to place, SL Order ID will be empty

---

## Risk & Position Sizing Explained

### How Quantity is Calculated (The Math)

```
Available Capital = ₹10,000
Entry Price (Titan) = ₹2,450
Stop Loss = ₹2,413.75 (1.5% below)
Risk per Trade = 2% of capital = ₹200

Distance from Entry to SL = ₹2,450 - ₹2,413.75 = ₹36.25

Ideal Quantity = Risk ÷ Distance
               = ₹200 ÷ ₹36.25
               = 5.51 shares

Affordable Quantity = (Capital × 20%) ÷ Entry Price
                    = (₹10,000 × 20%) ÷ ₹2,450
                    = ₹2,000 ÷ ₹2,450
                    = 0.82 shares

Final Qty = min(5.51, 0.82) = 0.82 → rounds to 0 shares
           ↓
           ❌ ORDER REJECTED (insufficient capital)
```

### ⚠️ Important: Capital Check

The system enforces:
```
IF (Final Qty < 1 share) 
    THEN Reject order completely (return qty=0)
    ELSE Place order with that quantity
```

**Before (BROKEN)**: Forced qty=1 even if couldn't afford it → **MARGIN BUY** ❌  
**After (FIXED)**: Rejects order if can't afford 1 share → **NO MARGIN** ✅

---

## Real Example: Your Titan Situation

```
🏦 Your Capital: ₹4,800 (available)
📊 Titan Stock: ₹2,450/share
📊 Other Stock: ₹1,300/share
🎯 Want to buy both?

Calculation:
- Titan needs ₹2,450 × 1 = ₹2,450 ✅
- After Titan buy: ₹4,800 - ₹2,450 = ₹2,350 left
- Other stock needs ₹1,300 × 1 = ₹1,300 ✅
- Total = ₹2,450 + ₹1,300 = ₹3,750 ✅ (less than ₹4,800)

✅ Both orders should execute!

But if total needed > ₹4,800:
❌ System rejects whichever order comes second
(No automatic margin forcing)
```

---

## Configuration Reference

These settings control order behavior:

| Setting | File | Purpose |
|---------|------|---------|
| `stop_loss_percent` | `trading_settings.json` | SL distance (default 1.5%) |
| `target_rr_ratio` | `trading_settings.json` | Risk:Reward target (default 2.5) |
| `max_risk_per_trade` | `trading_settings.json` | Max % of capital at risk (default 2%) |
| `product` | `order_executor.py` | Default = MIS (margin intraday) |
| `paper_capital` | `trading_settings.json` | Simulated capital for PAPER mode |

---

## Troubleshooting

### "Order Rejected - Insufficient Capital"
→ Your calculated position size is 0 shares (can't afford 1)  
→ **Fix**: Increase capital OR wait for cheaper stock OR increase risk% (not recommended)

### "SL Order Failed" 
→ Stop loss didn't place even though main BUY succeeded  
→ **Fix**: Check broker logs, may be token/session issue  
→ **Workaround**: Manually set SL in broker platform

### "Why Only 5 Shares When I Have ₹10,000?"
→ Risk management — system only risks 2% per trade  
→ 5 shares × SL distance = ₹180 risk = 1.8% of capital ✅  
→ Prevents blowout on bad trade

---

## Summary Checklist ✅

- [ ] Understand your **available capital** before each market open
- [ ] SL is **automatic** (broker will execute)
- [ ] TP is **manual** (you decide when to sell)
- [ ] **MIS orders close same day** (squareoff at 3:15 PM)
- [ ] **No margin forcing** — orders reject if you can't afford them
- [ ] **Risk per trade = 2%** of capital (editable in settings)
- [ ] **Always watch live prices** during your position holding time

---

**Questions?** Check audit logs in `data/audit/` or system logs in `data/alcosoft.log`
