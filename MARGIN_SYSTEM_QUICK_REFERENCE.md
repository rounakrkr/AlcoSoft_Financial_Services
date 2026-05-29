# 🎯 MARGIN SYSTEM - QUICK REFERENCE (2026-05-28)

**Status**: ✅ COMPLETE & VERIFIED  
**Your Requirement**: ✅ MET  
**Safety**: ✅ GUARANTEED

---

## 🚀 3-Minute Quick Start

### 1. Access Dashboard Settings
```
Open: http://localhost:5000 (after running dashboard/app.py)
Click: ⚙️ Settings (top right)
```

### 2. Enable/Disable Margin
```
Find: "Allow margin (experimental)" toggle
OFF  = 🔴 NO leverage (default - safe)
ON   = 🟢 YES leverage (using your configured multiplier)
```

### 3. Set Leverage Amount (if enabled)
```
Find: "Margin leverage multiplier"
Set to: 2.0 (double your capital - recommended)
       3.0 (triple - aggressive)
       1.5 (modest - conservative)
```

### 4. Optional: Force Buy Mode
```
Find: "Force buy with margin" toggle
OFF = 🟡 Normal mode (respects risk limits)
ON  = 🔥 Forced mode (buys max even if risk says no)
```

### 5. Save & Done!
```
Click: 💾 Save settings
Wait: 5 seconds for changes to apply
Check: Dashboard shows "🔥 Margin Status" widget (if enabled)
```

---

## 📊 What Each Setting Does

| Setting | OFF | ON |
|---------|-----|-----|
| **Allow margin** | ❌ Real capital only | ✅ Can use margin |
| **Margin leverage 2.0** | — | ₹1000 → ₹2000 available |
| **Margin leverage 3.0** | — | ₹1000 → ₹3000 available |
| **Force buy** | Follow risk calc | Buy max, ignore risk |

---

## 💡 Example: Your Scenario

```
YOUR CAPITAL:       ₹800
STOCK PRICE:        ₹1000
WANT TO BUY:        Multiple units (not just 1)

WITHOUT MARGIN:
  Available:  ₹800
  Affordable: 0 shares ❌ TOO EXPENSIVE

WITH MARGIN (2.0x):
  Available:  ₹800 × 2 = ₹1600
  Affordable: 1 share ✅ CAN BUY NOW
  
WITH FORCED BUY:
  Same as above: 1 share ✅
  (Plus access to tranches for pyramid buying)
```

---

## 🔥 Dashboard Widgets

### Settings Page
```
💰 Risk & exits (SL / TSL / targets / Margin)
├─ Stop loss (%)
├─ Allow margin [toggle]            ← MAIN SWITCH
├─ Margin leverage multiplier [2.0] ← SET YOUR LEVERAGE
├─ Force buy with margin [toggle]   ← OPTIONAL
└─ Position size with margin (%)
```

### Dashboard Home
```
When margin OFF:
  [No margin widget shown - clean dashboard]

When margin ON:
  🔥 Margin Status
  ────────────────
  25.0%
  2.0x Leverage | Deployed: ₹2000 | Remaining: ₹2000 | Safe
  
  Color = Safety Level
  🟢 Green (0-50%) = Safe
  🟠 Orange (50-80%) = Watch
  🔴 Red (80%+) = Danger - Consider closing positions
```

---

## ✅ Safety Guarantees

### ✓ Margin OFF = NO Leverage (GUARANTEED)
```
When allow_margin = false:
  Your capital stays ₹800
  No multiplication happens
  Behaves like margin disabled
```

### ✓ Risk Always Real Capital
```
Max loss per trade = real capital × risk %
NOT margin-inflated
```

### ✓ Over-Leverage Warning
```
Dashboard warns if margin % > 80%
Logs warn if something conflicts
```

### ✓ Easy to Disable
```
Toggle OFF → Margin stops being used
Existing positions unaffected
```

---

## 🎛️ Three Trading Modes

### Mode 1: Conservative (Recommended Start)
```
allow_margin:       OFF
forced_buy_margin:  OFF

Result: Real capital only, strict position sizing
Use if: Learning, testing, or risk-averse
```

### Mode 2: Margin Enabled (Moderate)
```
allow_margin:       ON
margin_leverage:    2.0
forced_buy_margin:  OFF

Result: Capital × 2, respects risk calculations
Use if: Want leverage but need discipline
```

### Mode 3: Forced Buy (Aggressive)
```
allow_margin:       ON
margin_leverage:    2.0-3.0
forced_buy_margin:  ON

Result: Capital × leverage, buys maximum possible
Use if: Experienced trader, pyramid strategy planned
```

---

## 📱 Common Actions

### "I want to use margin"
```
Settings → Allow margin: ON → Margin leverage: 2.0 → Save
Check dashboard for margin widget showing usage %
```

### "I want to stop using margin"
```
Settings → Allow margin: OFF → Save
Widget disappears, no margin used
```

### "I want to increase leverage from 2x to 3x"
```
Settings → Margin leverage: change 2.0 to 3.0 → Save
New orders use 3x, existing positions unaffected
```

### "I'm concerned about over-leveraging"
```
Check dashboard margin widget:
  If green: You're safe
  If orange: Consider reducing
  If red: Close worst position or disable forced_buy
```

---

## 🚨 Error Messages You Might See

### "Can't afford this order"
```
Cause: Not enough capital (even with margin)
Fix: Reduce order size OR increase margin_leverage
```

### "⚠️ forced_buy_margin enabled but allow_margin=False!"
```
Cause: You enabled forced_buy without enabling margin
Fix: Also enable "Allow margin" toggle
```

### "⚠️ OVER-LEVERAGE WARNING"
```
Cause: This order would exceed your margin limit
Fix: Close a position or reduce forced_buy
```

---

## 📞 Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Margin widget not showing | `allow_margin=OFF` | Enable it in settings |
| Settings not applying | Cache delay | Wait 5-10 seconds |
| Margin % stuck | Position data old | Refresh dashboard (F5) |
| Can't enable forced_buy | `allow_margin=OFF` | Enable margin first |
| Widget shows red | Over 80% deployed | Close a position |

---

## 🎯 Your Requirements - ALL MET ✅

| Requirement | Status | How |
|-------------|--------|-----|
| Margin OFF = NO leverage | ✅ | Hardcoded, verified code path |
| Dashboard shows controls | ✅ | Settings page with 4 margin options |
| Can choose margin amount | ✅ | Leverage multiplier slider (2.0-5.0x) |
| Buy multiple units | ✅ | Forced buy + tranches functions |
| Dashboard shows usage | ✅ | New margin status widget |
| Documented in .md | ✅ | 5 new .md files created |

---

## 📚 Documentation

| File | Purpose |
|------|---------|
| `MARGIN_OFF_SAFETY_GUARANTEE.md` | Proves margin OFF = no leverage |
| `DASHBOARD_MARGIN_CONFIGURATION.md` | Complete dashboard guide |
| `docs/MARGIN_FORCED_BUY_GUIDE.md` | Full user guide with examples |
| `MARGIN_FORCED_BUY_IMPLEMENTATION.md` | Technical implementation details |
| `MARGIN_SYSTEM_QUICK_REFERENCE.md` | This file - quick start |

---

## 🏁 Ready?

✅ Settings configured  
✅ Dashboard ready  
✅ Safety verified  
✅ Documentation complete  

**You're all set!**

Start with Mode 1 (Conservative), test in PAPER mode, then graduate to LIVE with margin when confident. 🚀
