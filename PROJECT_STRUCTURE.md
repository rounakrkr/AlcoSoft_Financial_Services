# 🏛️ AlcoSoft Financial Services — Project Structure Guide

**Last Updated**: 2026-05-28  
**Project Type**: Automated Trading & AI-Powered Analytics Platform  
**Tech Stack**: Python 3.10, Flask, SQLAlchemy, Gemini AI, AsyncIO, APScheduler

---

## 📁 Root Level Files

| File | Purpose |
|------|---------|
| **main.py** | 🚀 System entry point - schedules all jobs, initializes logging with emoji support, async event loop |
| **requirements.txt** | 📦 All Python dependencies (Flask, pandas, numpy, AI models, scheduling) |
| **populate_realistic_data.py** | 📊 Generates test data for development/demo purposes |
| **DASHBOARD_AUDIT_REPORT.md** | 📋 Complete audit of frontend & backend (validation report) |
| **PROJECT_STRUCTURE.md** | 📖 This file - complete architecture documentation |

---

## 📂 **core/** — Trading Engine & Core Logic

The heart of the automated trading system.

| Module | Purpose |
|--------|---------|
| **strategy.py** | 🎯 Main trading strategy - handles entry/exit logic, position sizing, risk management |
| **order_executor.py** | ⚡ Executes buy/sell orders on Kotak Neo API |
| **order_verifier.py** | ✅ Verifies order execution, reconciles fills, checks partial fills |
| **broker_reconciliation.py** | 🔄 Syncs order status with broker API, detects mismatches |
| **kotak_client.py** | 🔌 Wrapper for Kotak Neo broker API (login, order placement, position fetch) |
| **data_fetcher.py** | 📈 Fetches market data, OHLC candles, live quotes via yfinance/broker API |
| **token_validator.py** | 🏷️ Maps instrument names to broker tokens (e.g., "INFY" → token 737) |
| **market_calendar.py** | 📅 Checks if markets are open, handles holidays, session timings |
| **circuit_breaker.py** | 🛑 Stops trading if losses exceed threshold or volatility is extreme |
| **health_monitor.py** | 💚 Monitors system health (API connectivity, memory, CPU) |
| **state_manager.py** | 💾 Persists trading state to disk (positions, capital, logs) |
| **alerts.py** | 🔔 Sends Telegram/email alerts for order fills, risk breaches |
| **api_resilience.py** | 🔁 Retry logic + circuit breaker for API calls (network resilience) |
| **audit_logger.py** | 📝 Logs all trades to JSONL audit files in `data/audit/` |
| **trading_settings.py** | ⚙️ Loads config from `config/trading_settings.json` |

---

## 🤖 **war_room/** — Multi-Agent AI Decision Engine

Autonomous AI agents that analyze market conditions and suggest trades.

| File | Purpose |
|------|---------|
| **orchestrator.py** | 🎭 Coordinates all agents, collects signals, makes final trade decision |
| **agents/base_agent.py** | 🧠 Base class for all AI agents (Gemini API wrapper) |
| **agents/technical.py** | 📊 Technical analysis agent (RSI, MACD, support/resistance levels) |
| **agents/fundamental.py** | 📰 Fundamental analysis agent (earnings, P/E, sector trends) |
| **agents/risk.py** | ⚠️ Risk assessment agent (volatility, portfolio risk, correlation) |
| **agents/mediator.py** | 🤝 Mediator agent (consensus builder - weighs signals from all agents) |
| **prompts/** | Contains LLM prompts for each agent (technical.txt, fundamental.txt, etc.) |

---

## 📺 **dashboard/** — Web Interface & Real-Time Monitoring

Flask-based web dashboard for monitoring & manual control.

```
dashboard/
├── app.py                    # Flask app with API routes (/api/status, /api/settings)
├── templates/
│   ├── index.html           # 📊 Main dashboard (live trading status, charts, logs)
│   └── settings.html        # ⚙️ Settings form (strategy parameters, risk limits)
└── static/
    ├── css/
    │   ├── style.css        # Main styling (responsive grid, light theme)
    │   └── charts.css       # Chart.js styling
    ├── js/
    │   ├── app.js           # Dashboard logic (5s polling, status updates)
    │   ├── settings.js      # Settings form builder
    │   └── charts.js        # Chart rendering (Chart.js library)
```

**API Endpoints**:
- `GET /` → Render dashboard
- `GET /settings` → Render settings page
- `GET /api/status` → Live trading status (JSON)
- `GET /api/settings` → Current config + schema
- `POST /api/settings` → Update configuration

---

## 📂 **screener/** — Market Screener

Identifies trading opportunities at market open.

| File | Purpose |
|------|---------|
| **morning_screener.py** | 🔍 Scans market (volume, volatility, moving averages) at 9:15 AM IST, generates watchlist |

---

## 📚 **reflection/** — Learning & Optimization Loop

AI-powered feedback system to improve strategy over time.

| File | Purpose |
|------|---------|
| **reflection_loop.py** | 🔄 Analyzes past trades, learns from wins/losses, updates strategy parameters |

---

## 📊 **config/** — Configuration Files

| File | Purpose |
|------|---------|
| **trading_settings.json** | ⚙️ Strategy parameters (position size, stop-loss %, risk limits, symbol list) |

---

## 💾 **data/** — Runtime Data & Audit Logs

| File | Purpose |
|------|---------|
| **instrument_tokens.json** | 🏷️ Mapping of stock symbols to broker token IDs |
| **trading_settings.json** | 📋 Current strategy configuration |
| **session_briefing.json** | 📈 Today's screener results + watchlist |
| **live_capital.json** | 💰 Current available capital, margin used |
| **positions.json** | 📍 Open positions (quantity, entry price, current P&L) |
| **learnings.json** | 📚 Historical patterns learned from past trades |
| **audit/** | 📝 JSONL audit logs per date (e.g., `2026-05-27.jsonl`) |
| **reflections/** | 🤔 Reflection analysis logs from learning loop |

---

## 🔄 **alco_env/** — Python Virtual Environment

Isolated Python 3.10 environment with all dependencies pre-installed.

---

## 🚀 How It All Works

```
┌─────────────────────────────────────────────────────────┐
│                  python main.py                          │
│         (Entry Point - Starts Everything)               │
└─────────────┬───────────────────────────────────────────┘
              │
              ├─→ APScheduler (AsyncIO)
              │   ├─ 9:15 AM IST  → morning_screener.py
              │   ├─ Every 5 min  → war_room/orchestrator.py (agent consensus)
              │   ├─ Every tick   → order_executor.py (execute trades)
              │   └─ EOD 3:30 PM  → reflection_loop.py (learn & optimize)
              │
              ├─→ Flask Dashboard (Port 5000)
              │   ├─ Real-time status updates (5s polling)
              │   ├─ Chart rendering (P&L, win/loss)
              │   └─ Manual settings override
              │
              ├─→ Core Trading Engine
              │   ├─ Broker API (Kotak Neo)
              │   ├─ Order execution & verification
              │   ├─ Position tracking & reconciliation
              │   └─ Risk monitoring & alerts
              │
              └─→ Audit & State Management
                  ├─ JSONL audit logs
                  └─ State persistence (positions, capital, learnings)
```

---

## 📋 Quick Start

```bash
# 1. Activate environment
.\alco_env\Scripts\activate

# 2. Start system
python main.py

# 3. Open dashboard
http://localhost:5000

# 4. View logs
tail -f alcosoft.log
```

---

## 🎯 Key Features Summary

✅ **Automated Trading** - AI agents make data-driven decisions  
✅ **Real-time Dashboard** - Live monitoring & manual control  
✅ **Risk Management** - Circuit breaker + position sizing  
✅ **Audit Trail** - Every trade logged for compliance  
✅ **Learning Loop** - System improves strategy over time  
✅ **Multi-agent Consensus** - Technical + Fundamental + Risk analysis  
✅ **Market-aware** - Respects market hours & holidays  
✅ **Resilient APIs** - Retry logic for network issues  

---

**Team**: AlcoSoft Financial Services  
**Status**: Production-Ready  
**Last Run**: 2026-05-28 01:13 IST
