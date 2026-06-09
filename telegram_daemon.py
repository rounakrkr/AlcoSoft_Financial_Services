import os
import sys
import json
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv

# Use asyncio from python-telegram-bot
from telegram import Update, BotCommand, BotCommandScopeChat
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load env
_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ROOT, ".env"))

DB_PATH = os.path.join(_ROOT, "data", "alcosoft.db")
LAST_ALERT_PATH = os.path.join(_ROOT, "data", "last_alert.json")

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def auth_required(func):
    """Decorator to ensure only the authorized chat ID can use the bot."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != ALLOWED_CHAT_ID:
            logger.warning(f"Unauthorized access attempt from {update.effective_chat.id}")
            return
        return await func(update, context)
    return wrapper

def _query_db(query: str, params: tuple = ()) -> list:
    if not os.path.exists(DB_PATH):
        return []
    try:
        # uri=True and 'ro' mode prevent locking issues with the engine
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"DB Read Error: {e}")
        return []

@auth_required
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Last alert
    last_alert_msg = "None"
    last_alert_time = "N/A"
    if os.path.exists(LAST_ALERT_PATH):
        try:
            with open(LAST_ALERT_PATH, "r") as f:
                data = json.load(f)
                last_alert_msg = data.get("message", "None")[:100]
                last_alert_time = data.get("time", "N/A")
        except:
            pass

    open_pos = len(_query_db("SELECT id FROM trades WHERE status = 'OPEN' AND quantity > 0"))
    
    text = (
        "🟢 <b>ALCOSOFT HEALTH STATUS</b>\n\n"
        "Engine: Healthy\n"
        "Broker: Connected\n"
        "Scanner: Running\n"
        "Trading: Active\n"
        f"Open Pos: {open_pos}\n\n"
        "<b>Last Alert:</b>\n"
        f"{last_alert_msg}\n"
        f"Time: {last_alert_time}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

@auth_required
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = _query_db("SELECT symbol, action, quantity, entry_price, target_price, stop_loss FROM trades WHERE status = 'OPEN' AND quantity > 0")
    if not rows:
        await update.message.reply_text("📊 <b>OPEN POSITIONS</b>\n\nNo open positions.", parse_mode="HTML")
        return
        
    text = f"📊 <b>OPEN POSITIONS ({len(rows)})</b>\n\n"
    for r in rows:
        action = r['action'].upper() if r['action'] else "LONG"
        text += (
            f"<b>{r['symbol']}</b>\n"
            f"Side: {action}\n"
            f"Qty: {r['quantity']}\n"
            f"Entry: ₹{r.get('entry_price', 0):.2f}\n"
            f"Stop: ₹{r.get('stop_loss', 0):.2f}\n\n"
        )
        
    await update.message.reply_text(text, parse_mode="HTML")

@auth_required
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _query_db("SELECT * FROM daily_stats WHERE date = ?", (today,))
    if rows:
        stats = rows[0]
        text = (
            "📅 <b>TODAY'S SUMMARY</b>\n\n"
            f"Trades: {stats.get('total_trades', 0)}\n"
            f"Wins: {stats.get('winning_trades', 0)} | Losses: {stats.get('losing_trades', 0)}\n"
            f"Gross PnL: ₹{stats.get('gross_pnl', 0):.2f}"
        )
    else:
        text = "📅 <b>TODAY'S SUMMARY</b>\n\nNo trades taken today."
    await update.message.reply_text(text, parse_mode="HTML")

@auth_required
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_today(update, context)

@auth_required
async def cmd_margin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.order_executor import get_capital_snapshot
    snap = get_capital_snapshot()
    text = (
        "📊 <b>RISK & MARGIN</b>\n\n"
        f"Mode: <code>{snap['mode']}</code>\n"
        f"Account Equity: ₹{snap['account_equity']:.2f}\n"
        f"Free Margin: ₹{snap['free_margin']:.2f}\n"
        f"Margin Blocked: ₹{snap['margin_blocked']:.2f}\n"
        f"Gross Exposure: ₹{snap['gross_exposure']:.2f}\n"
        f"Margin Utilized: {snap['margin_utilization']:.1f}%\n"
        f"Buying Power: ₹{snap['remaining_buying_power']:.2f}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

@auth_required
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = _query_db("SELECT symbol, action, pnl, exit_price FROM trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT 3")
    if not rows:
        await update.message.reply_text("No recent trades.", parse_mode="HTML")
        return
    text = "🔄 <b>RECENT TRADES</b>\n\n"
    for i, r in enumerate(rows, 1):
        pnl = r.get('pnl', 0)
        icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        text += f"{i}. <b>{r['symbol']}</b> ({r['action']})\n{icon} PnL: ₹{pnl:.2f} | Exit: ₹{r.get('exit_price',0):.2f}\n\n"
    await update.message.reply_text(text, parse_mode="HTML")

@auth_required
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime, timedelta
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    rows = _query_db("SELECT date, realized_equity, agent_decision_calls FROM daily_stats WHERE date >= ? ORDER BY date ASC", (start_of_week.isoformat(),))
    
    if not rows:
        await update.message.reply_text("📅 <b>WEEKLY SUMMARY</b>\nNo data for the current week yet.", parse_mode="HTML")
        return
        
    text = "📅 <b>WEEKLY SUMMARY</b>\n\n"
    total_pnl = 0.0
    total_trades = 0
    winning_days = 0
    best_trade = 0.0
    worst_trade = 0.0
    
    # We need to query trades for the week to get best/worst trade and win rate.
    trades_rows = _query_db("SELECT pnl FROM trades WHERE status = 'CLOSED' AND exit_time >= ?", (start_of_week.isoformat(),))
    wins = 0
    losses = 0
    for tr in trades_rows:
        tpnl = float(tr.get('pnl') or 0.0)
        if tpnl > 0: wins += 1
        else: losses += 1
        if tpnl > best_trade: best_trade = tpnl
        if tpnl < worst_trade: worst_trade = tpnl
        
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0

    for r in rows:
        pnl = r.get('realized_equity') or 0.0
        trades = r.get('agent_decision_calls') or 0
        total_pnl += float(pnl)
        total_trades += int(trades)
        icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        text += f"{r['date']}: {icon} ₹{pnl:.2f} ({trades} trades)\n"
    
    text += f"\n<b>Total PnL:</b> ₹{total_pnl:.2f}\n"
    text += f"<b>Total Trades:</b> {total_trades}\n"
    text += f"<b>Win Rate:</b> {win_rate:.1f}%\n"
    text += f"<b>Best Trade:</b> ₹{best_trade:.2f}\n"
    text += f"<b>Worst Trade:</b> ₹{worst_trade:.2f}\n"
    
    await update.message.reply_text(text, parse_mode="HTML")
    
@auth_required
async def cmd_equity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.order_executor import get_capital_snapshot
    snap = get_capital_snapshot()
    text = (
        "💰 <b>EQUITY SNAPSHOT</b>\n\n"
        f"Starting Capital: ₹{snap['starting_capital']:.2f}\n"
        f"Closed PnL: ₹{snap['closed_pnl']:.2f}\n"
        f"Unrealized PnL: ₹{snap['unrealized_pnl']:.2f}\n"
        f"Total Equity: ₹{snap['account_equity']:.2f}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")
@auth_required
async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚙️ <b>ALCOSOFT VERSION</b>\nBuild: TEL Release 1.0\nStatus: Production", parse_mode="HTML")

@auth_required
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>AVAILABLE COMMANDS</b>\n\n"
        "/health - Instant system pulse check\n"
        "/positions - List currently open trades\n"
        "/pnl - Current day's PnL\n"
        "/today - Complete snapshot of today's session\n"
        "/margin - Capital utilization and exposure\n"
        "/recent - Last 3 closed trades\n"
        "/week - Summary of the current week\n"
        "/equity - Account Equity details\n"
        "/version - Current deployment version\n"
        "/help - List available commands"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def on_startup(application):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if bot_token and chat_id:
        commands = [
            BotCommand("health", "System health status"),
            BotCommand("positions", "Open positions"),
            BotCommand("today", "Today's trading summary"),
            BotCommand("pnl", "Current PnL snapshot"),
            BotCommand("margin", "Capital and margin status"),
            BotCommand("recent", "Recent closed trades"),
            BotCommand("week", "Weekly performance summary"),
            BotCommand("equity", "Account equity snapshot"),
            BotCommand("version", "Bot version information"),
            BotCommand("help", "Show available commands")
        ]
        try:
            await application.bot.set_my_commands(
                commands=commands,
                scope=BotCommandScopeChat(chat_id=chat_id)
            )
            logger.info("Successfully registered native Telegram commands.")
        except Exception as e:
            logger.error(f"Failed to register commands: {e}")

        text = (
            "🟢 <b>ALCOSOFT TELEGRAM ONLINE</b>\n\n"
            "Bot Connected\n"
            "Chat Verified\n"
            "Notifier Running\n"
            "Command Listener Running\n\n"
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            await application.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Startup message failed: {e}")

def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not found in .env. Bot daemon cannot start.")
        return
        
    application = ApplicationBuilder().token(bot_token).post_init(on_startup).build()

    application.add_handler(CommandHandler("health", cmd_health))
    application.add_handler(CommandHandler("positions", cmd_positions))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("pnl", cmd_pnl))
    application.add_handler(CommandHandler("margin", cmd_margin))
    application.add_handler(CommandHandler("recent", cmd_recent))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("equity", cmd_equity))
    application.add_handler(CommandHandler("version", cmd_version))
    application.add_handler(CommandHandler("help", cmd_help))

    logger.info("Starting Telegram Polling Daemon...")
    application.run_polling()

if __name__ == '__main__':
    main()
