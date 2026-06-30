import json
import os

transcript_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\acbe5f97-2544-4468-b175-d002090fd989\.system_generated\logs\transcript_full.jsonl"
out_path = r"C:\Extra Programs\Files\AlcoSoft_Financial_Services\research\long_report_dump.txt"

print("Searching transcript for long report options...")
with open(out_path, 'w', encoding='utf-8') as out_f:
    with open(transcript_path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            if "BUY_STREAK" in line or "Long Engine" in line or "LONG" in line:
                data = json.loads(line)
                if data.get("type") == "PLANNER_RESPONSE" and data.get("source") == "MODEL":
                    content = data.get("content", "")
                    if "Win Rate" in content and "Trades" in content:
                        out_f.write(f"--- Step {data.get('step_index')} ---\n")
                        out_f.write(content + "\n")
                        out_f.write("\n" + "="*50 + "\n")
