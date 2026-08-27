#!/usr/bin/env python3
"""Example skill script — executed via run_skill_script.

This script is discovered as `scripts/hello.py` for the
`k8s-diag` skill (pydantic-ai-skills scans root + scripts/ subdir).
The agent calls it with:
  run_skill_script(skill_name="k8s-diag", script_name="scripts/hello.py", args={"message": "hi"})

Args are passed as CLI flags: --message "hi"
"""
import argparse
import json
import sys

def main() -> None:
    parser = argparse.ArgumentParser(description="k8s-diag hello script")
    parser.add_argument("--message", default="hello from k8s-diag skill script", help="message to echo")
    parser.add_argument("--cluster", default="unknown", help="cluster name")
    args = parser.parse_args()

    result = {
        "skill": "k8s-diag",
        "script": "scripts/hello.py",
        "message": args.message,
        "cluster": args.cluster,
        "status": "executed",
        "python": sys.version.split()[0],
    }
    # run_skill_script returns stdout as string — print JSON so the agent can parse it
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
