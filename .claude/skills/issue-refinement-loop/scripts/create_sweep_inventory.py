#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

# Target candidates from Issue #395
TARGET_ISSUES = [151, 203, 220, 228, 252, 300, 185, 186, 30, 247, 248, 339, 389]

def run_gh_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()

def get_issue_info(issue_number: int) -> dict:
    cmd = ["gh", "issue", "view", str(issue_number), "--json", "title,state,url"]
    try:
        output = run_gh_cmd(cmd)
        return json.loads(output)
    except subprocess.CalledProcessError as e:
        print(f"Error fetching issue {issue_number}: {e}", file=sys.stderr)
        return {}

def main():
    print("Fetching issue information...", file=sys.stderr)
    inventory = []
    for issue_number in TARGET_ISSUES:
        info = get_issue_info(issue_number)
        if not info:
            continue
        
        # Pre-fill structure for CLOSE_DECISION_V1
        state = info.get("state")
        decision = "still_needed"
        github_close_state = None
        evidence = None

        if state == "CLOSED":
            # If already closed, we might categorize it as absorbed or superseded based on our knowledge,
            # but for the inventory we can just put a placeholder or skip closing them.
            # However, the schema requires evidence for absorbed.
            decision = "absorbed"
            github_close_state = "completed"
            evidence = {"pr_numbers": [], "file_paths": [], "ac_vc_refs": []}

        decision_item = {
            "issue_number": issue_number,
            "title": info.get("title"),
            "current_state": state,
            "decision": decision,
            "github_close_state": github_close_state,
            "reasoning": "Pending manual review."
        }
        
        if evidence is not None:
            decision_item["evidence"] = evidence
            
        inventory.append(decision_item)

    output_path = Path(".claude/skills/issue-refinement-loop/scripts/sweep_inventory.json")
    output_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False))
    print(f"Created inventory at {output_path}", file=sys.stderr)

if __name__ == "__main__":
    main()
