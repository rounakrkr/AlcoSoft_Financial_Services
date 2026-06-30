import re

log_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\d0e7b991-cd8d-488b-8e89-236d6d3eb3a3\.system_generated\tasks\task-1436.log"
out_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\d0e7b991-cd8d-488b-8e89-236d6d3eb3a3\monthly_comparison_report.md"

with open(log_path, "r", encoding="utf-8") as f:
    text = f.read()

# Split into universes
u1_match = re.search(r"UNIVERSE 1: NIFTY 50 \(48 stocks\)\n.*?\n(.*?)TOTAL\s+([^\n]+)", text, re.DOTALL)
u2_match = re.search(r"UNIVERSE 2: NIFTY NEXT 50 \(42 stocks\)\n.*?\n(.*?)TOTAL\s+([^\n]+)", text, re.DOTALL)
u3_match = re.search(r"UNIVERSE 3: NIFTY 100 \(90 stocks\)\n.*?\n(.*?)TOTAL\s+([^\n]+)", text, re.DOTALL)

def format_table(match_obj):
    rows = match_obj.group(1).strip().split('\n')
    total_line = match_obj.group(2).strip()
    
    table = "| Month | Trades | Long | Short | Win % | Gross % | STT % | Net % | Cumulative |\n"
    table += "|---|---|---|---|---|---|---|---|---|\n"
    
    for row in rows:
        if "---" in row or "Month" in row: continue
        parts = re.split(r'\s+', row.strip())
        if len(parts) >= 9:
            table += f"| {parts[0]} | {parts[1]} | {parts[2]} | {parts[3]} | {parts[4]} | {parts[5]} | {parts[6]} | **{parts[7]}** | {parts[8]} |\n"
        elif parts[1] == "--":
             table += f"| {parts[0]} | - | - | - | - | - | - | - | - |\n"

    t_parts = re.split(r'\s+', total_line)
    table += f"| **TOTAL** | **{t_parts[0]}** | **{t_parts[1]}** | **{t_parts[2]}** | **{t_parts[3]}** | **{t_parts[4]}** | **{t_parts[5]}** | **{t_parts[6]}** | - |\n"
    return table

md = "# 📅 AlcoSoft Dual-Engine: 2.5 Year Monthly Breakdown (Jan 2024 - Jun 2026)\n\n"
md += "> [!CAUTION]\n> **SEVERE DRAWDOWN ALERT:** The strategy exhibits massive degradation outside the original Jan-Jun 2026 optimization window. High trade frequency combined with STT drag has resulted in a net loss across all universes over the 2.5 year period.\n\n"

md += "## 🔵 Universe 1: NIFTY 50\n\n"
md += format_table(u1_match) + "\n\n"

md += "## 🟠 Universe 2: NIFTY NEXT 50\n\n"
md += format_table(u2_match) + "\n\n"

md += "## 🟣 Universe 3: NIFTY 100\n\n"
md += format_table(u3_match) + "\n\n"

with open(out_path, "w", encoding="utf-8") as f:
    f.write(md)
