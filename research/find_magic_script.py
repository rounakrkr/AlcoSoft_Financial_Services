import json

transcript_path = r"C:\Users\RounakKR\.gemini\antigravity\brain\acbe5f97-2544-4468-b175-d002090fd989\.system_generated\logs\transcript_full.jsonl"
out_path = r"C:\Extra Programs\Files\AlcoSoft_Financial_Services\research\find_magic_script.txt"

commands = []
with open(transcript_path, 'r', encoding='utf-8') as f:
    for line in f:
        data = json.loads(line)
        step = data.get("step_index")
        
        # Track all run_command and write_to_file calls before 3800
        if step and step < 3800:
            if data.get("type") == "PLANNER_RESPONSE":
                tool_calls = data.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("name") in ["run_command", "write_to_file", "replace_file_content"]:
                        commands.append((step, tc))
                        
            # Also track task outputs
            if data.get("type") == "SYSTEM":
                content = data.get("content", "")
                if "236" in content and "63.1%" in content:
                    commands.append((step, "FOUND_THE_OUTPUT", content))

with open(out_path, 'w', encoding='utf-8') as out:
    for step, tc, *content in commands[-50:]:  # last 50 actions before step 3800
        out.write(f"--- Step {step} ---\n")
        if isinstance(tc, dict) and "function" in tc:
            func = tc["function"]
            out.write(f"Tool: {func.get('name')}\n")
            try:
                args = json.loads(func.get("arguments", "{}"))
                for k, v in args.items():
                    out.write(f"{k}: {v}\n")
            except:
                out.write(f"Args: {func.get('arguments')}\n")
        else:
            out.write(f"{tc}\n")
            if content:
                out.write(f"Content: {content[0][:500]}...\n")
        out.write("\n")
