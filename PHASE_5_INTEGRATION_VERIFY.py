#!/usr/bin/env python3
# ============================================================
#   ALCOSOFT FINANCIAL SERVICES
#   PHASE_5_INTEGRATION_VERIFY.py — Verification Script
#
#   Verifies all Phase 5 architectural updates are in place.
#   Run before deployment: python PHASE_5_INTEGRATION_VERIFY.py
# ============================================================

import os
import sys
import re
from pathlib import Path
from datetime import datetime, time as dt_time

# ════════════════════════════════════════════════════════════
#   VERIFICATION TESTS
# ════════════════════════════════════════════════════════════

class Phase5Verifier:
    def __init__(self):
        self.root = Path(__file__).parent
        self.tests_passed = 0
        self.tests_failed = 0
        self.warnings = []

    def check_file_exists(self, path: str) -> bool:
        """Verify file exists."""
        full_path = self.root / path
        if full_path.exists():
            print(f"  ✅ {path}")
            return True
        else:
            print(f"  ❌ {path} (NOT FOUND)")
            self.tests_failed += 1
            return False

    def check_file_contains(self, path: str, pattern: str, label: str) -> bool:
        """Verify file contains pattern."""
        full_path = self.root / path
        if not full_path.exists():
            print(f"  ❌ {label} (file not found)")
            self.tests_failed += 1
            return False

        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            if re.search(pattern, content, re.IGNORECASE | re.MULTILINE):
                print(f"  ✅ {label}")
                self.tests_passed += 1
                return True
            else:
                print(f"  ❌ {label} (pattern not found)")
                self.tests_failed += 1
                return False

    def run_all_checks(self):
        """Run all verification checks."""
        print("\n" + "="*60)
        print("PHASE 5 ARCHITECTURAL UPDATES — VERIFICATION")
        print("="*60)

        # 1. File Existence Checks
        print("\n[1/6] FILE STRUCTURE")
        print("─" * 60)

        files_required = [
            "reflection/reflection_loop.py",
            "reflection/cognitive_agents.py",
            "reflection/cognition_engine.py",
            "reflection/cognition_llm_client.py",
            "reflection/cognition_scheduler.py",
            "reflection/observation_loop.py",
            "dashboard/app.py",
            "dashboard/cognition_lab.py",
            "main.py",
        ]

        for file_path in files_required:
            self.check_file_exists(file_path)

        # 2. Timing Refinement (3:15 PM cognition cutoff)
        print("\n[2/6] TIMING REFINEMENT — Cognition Cutoff at 3:15 PM")
        print("─" * 60)

        self.check_file_contains(
            "reflection/cognition_scheduler.py",
            r"last_cycle\s*=\s*dt_time\(15,\s*15\)",
            "Cognition scheduler: last_cycle at 3:15 PM"
        )

        self.check_file_contains(
            "reflection/cognition_scheduler.py",
            r"now\s*>\s*last_cycle",
            "Cognition scheduler: time check against last_cycle"
        )

        # 3. Market-Tied Cognition Documentation
        print("\n[3/6] MARKET-TIED COGNITION DOCUMENTATION")
        print("─" * 60)

        self.check_file_contains(
            "reflection/cognition_scheduler.py",
            r"MARKET-TIED COGNITION",
            "Market-tied cognition explanation in scheduler"
        )

        self.check_file_contains(
            "reflection/reflection_loop.py",
            r"3:15 PM.*Last cognition.*observation",
            "Last cognition timing in reflection_loop"
        )

        # 4. Ollama Integration Verification
        print("\n[4/6] OLLAMA/LOCAL MODEL INTEGRATION")
        print("─" * 60)

        self.check_file_contains(
            "reflection/cognition_llm_client.py",
            r"def _call_ollama",
            "Ollama provider implementation"
        )

        self.check_file_contains(
            "reflection/cognition_llm_client.py",
            r"OLLAMA_BASE_URL.*localhost:11434",
            "Ollama default endpoint configuration"
        )

        self.check_file_contains(
            "reflection/cognition_llm_client.py",
            r"PREFERRED_PROVIDER.*openrouter.*ollama.*auto",
            "Provider selection configuration"
        )

        # 5. Safe First-Cycle Initialization
        print("\n[5/6] SAFE FIRST-CYCLE INITIALIZATION")
        print("─" * 60)

        self.check_file_contains(
            "reflection/cognitive_agents.py",
            r"SAFE FOR FIRST CYCLE",
            "First-cycle safety documentation in agents"
        )

        self.check_file_contains(
            "reflection/cognitive_agents.py",
            r"previous_cycles and len\(previous_cycles\) > 0",
            "Safe empty history handling"
        )

        self.check_file_contains(
            "reflection/cognitive_agents.py",
            r"No previous observations - first trading cycle",
            "First cycle fallback message"
        )

        # 6. Cognition Lab Dashboard Integration
        print("\n[6/6] COGNITION LAB DASHBOARD INTEGRATION")
        print("─" * 60)

        self.check_file_contains(
            "dashboard/app.py",
            r"cognition_lab.*Blueprint",
            "Cognition Lab blueprint definition imported"
        )

        self.check_file_contains(
            "dashboard/app.py",
            r"app\.register_blueprint\(cognition_lab\)",
            "Cognition Lab blueprint registered"
        )

        self.check_file_contains(
            "dashboard/cognition_lab.py",
            r"@cognition_lab\.route\(['\"]\/status",
            "Cognition Lab status endpoint"
        )

        # 7. Scheduler Integration
        print("\n[7/7] COGNITION SCHEDULER INTEGRATION")
        print("─" * 60)

        self.check_file_contains(
            "reflection/observation_loop.py",
            r"schedule_cognitive_cycle",
            "Cognition scheduler call in observation loop"
        )

        self.check_file_contains(
            "reflection/observation_loop.py",
            r"INTEGRATION.*Trigger cognitive agents",
            "Scheduler integration comment in observation loop"
        )

        # Print Summary
        print("\n" + "="*60)
        print("VERIFICATION SUMMARY")
        print("="*60)

        total = self.tests_passed + self.tests_failed
        print(f"\n✅ Passed:  {self.tests_passed}")
        print(f"❌ Failed:  {self.tests_failed}")
        print(f"📊 Total:   {total}")

        if self.warnings:
            print(f"\n⚠️  WARNINGS ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"   - {warning}")

        print("\n" + "="*60)

        if self.tests_failed == 0:
            print("✅ PHASE 5 VERIFICATION COMPLETE — ALL CHECKS PASSED")
            print("   Ready for deployment!")
            return True
        else:
            print(f"❌ PHASE 5 VERIFICATION FAILED — {self.tests_failed} issue(s)")
            print("   Please fix issues above before deployment.")
            return False

        print("="*60 + "\n")


# ════════════════════════════════════════════════════════════
#   TIMING ANALYSIS
# ════════════════════════════════════════════════════════════

def analyze_timing():
    """Analyze and display the Phase 5 timing schedule."""
    print("\n" + "="*60)
    print("PHASE 5 TIMING SCHEDULE")
    print("="*60)

    print("\nCOGNITION SCHEDULE (9:30 AM - 3:15 PM every 15 min):")
    print("─" * 60)

    times = [
        ("9:30 AM", "Agent A — Market Structure Observer"),
        ("9:45 AM", "Agent B — Signal Performance Analyst"),
        ("10:00 AM", "Agent C — Regime Transition Specialist"),
        ("10:15 AM", "Agent D — Meta-Pattern Synthesizer"),
        ("10:30 AM", "Agent A again..."),
        ("...", "...continues every 15 minutes..."),
        ("3:00 PM", "⚠️  EXECUTION STOPS (Cognition continues)"),
        ("3:15 PM", "✅ LAST COGNITION CYCLE"),
        ("3:30 PM", "Market officially closes"),
        ("3:35 PM", "🦉 FINAL REFLECTION SYNTHESIS"),
    ]

    for time, desc in times:
        print(f"  {time:>10}  {desc}")

    print("\n" + "="*60)


# ════════════════════════════════════════════════════════════
#   ENV CONFIGURATION CHECK
# ════════════════════════════════════════════════════════════

def check_env_config():
    """Check .env configuration for cognition LLM."""
    print("\n" + "="*60)
    print("ENVIRONMENT CONFIGURATION CHECK")
    print("="*60)

    env_path = Path(".env")

    if not env_path.exists():
        print("\n⚠️  .env file not found (optional — defaults will be used)")
        return

    print("\nConfiguration options (in .env):\n")

    with open(env_path, 'r') as f:
        content = f.read()

    # Check for cognition-related settings
    configs = {
        "COGNITION_LLM_PROVIDER": "LLM Provider selection (openrouter/ollama/auto)",
        "OPENROUTER_KEY_2": "OpenRouter API key for cognition agents",
        "OLLAMA_BASE_URL": "Ollama endpoint (http://localhost:11434)",
        "OLLAMA_MODEL": "Ollama model name (mistral-small, qwen2.5:7b, etc.)",
    }

    found_any = False
    for key, desc in configs.items():
        if key in content:
            print(f"  ✅ {key}")
            print(f"     {desc}")
            found_any = True

    if not found_any:
        print("  ℹ️  No cognition-specific settings found (will use defaults)")
        print("  - OpenRouter will be tried first")
        print("  - Ollama will be fallback")

    print("\n" + "="*60)


# ════════════════════════════════════════════════════════════
#   MAIN
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    verifier = Phase5Verifier()

    # Run verification
    success = verifier.run_all_checks()

    # Display timing
    analyze_timing()

    # Check env config
    check_env_config()

    # Final status
    print("\n" + "="*60)
    if success:
        print("✅ PHASE 5 READY FOR DEPLOYMENT")
        print("\nNext steps:")
        print("  1. python main.py (start trading with Phase 5 features)")
        print("  2. Monitor: tail -f data/alcosoft.log | grep 🧠")
        print("  3. Visit: http://localhost:5000/cognition/status")
    else:
        print("❌ PHASE 5 VERIFICATION FAILED")
        print("\nFix the issues above and run verification again.")
        sys.exit(1)
    print("="*60 + "\n")
