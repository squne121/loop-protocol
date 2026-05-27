#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

def run_gh_cmd(cmd: list[str]) -> str:
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running cmd: {result.stderr}", file=sys.stderr)
    return result.stdout.strip()

def post_comment(issue_number: int, body: str):
    cmd = ["gh", "issue", "comment", str(issue_number), "-b", body]
    run_gh_cmd(cmd)

def close_issue(issue_number: int, reason: str):
    cmd = ["gh", "issue", "close", str(issue_number), "-r", reason]
    run_gh_cmd(cmd)

def main():
    inventory_path = Path(".claude/skills/issue-refinement-loop/scripts/sweep_inventory.json")
    if not inventory_path.exists():
        print("Inventory not found.", file=sys.stderr)
        sys.exit(1)

    with open(inventory_path, "r", encoding="utf-8") as f:
        inventory = json.load(f)

    summary_lines = ["### issue-refinement-loop Sweep Summary (#395)\n"]

    for item in inventory:
        issue_number = item["issue_number"]
        decision = item["decision"]
        state = item.get("current_state")
        github_close_state = item.get("github_close_state")
        reasoning = item.get("reasoning")
        evidence = item.get("evidence")

        summary_lines.append(f"- #{issue_number}: **{decision}** (was {state})")

        # Skip if already closed
        if state == "CLOSED":
            continue

        if decision in ["absorbed", "superseded"]:
            # Format comment
            comment_body = f"### Decision: {decision.capitalize()}\n\n"
            comment_body += f"**Reasoning**: {reasoning}\n"
            if decision == "absorbed" and evidence:
                comment_body += "\n**Evidence**:\n"
                if evidence.get("pr_numbers"):
                    comment_body += "- PRs: " + ", ".join(f"#{pr}" for pr in evidence["pr_numbers"]) + "\n"
                if evidence.get("file_paths"):
                    comment_body += "- Paths: " + ", ".join(f"`{p}`" for p in evidence["file_paths"]) + "\n"
                if evidence.get("ac_vc_refs"):
                    comment_body += "- AC/VC Refs: " + ", ".join(evidence["ac_vc_refs"]) + "\n"
            
            post_comment(issue_number, comment_body)
            
            if github_close_state == "completed":
                close_issue(issue_number, "completed")
            elif github_close_state == "not_planned":
                close_issue(issue_number, "not planned")

        elif decision in ["still_needed", "blocked"]:
            comment_body = f"### Decision: {decision.replace('_', ' ').capitalize()}\n\n"
            comment_body += f"**Reasoning**: {reasoning}\n"
            post_comment(issue_number, comment_body)

    # Post summary to #391 (Parent tracker)
    post_comment(391, "\n".join(summary_lines))
    print("Sweep applied successfully.", file=sys.stderr)

if __name__ == "__main__":
    main()
