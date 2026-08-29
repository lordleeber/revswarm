#!/usr/bin/env python3
"""PreToolUse hook: block Read on data/title_review.csv.

The file is append-only and runs to tens of thousands of lines; reading it
whole would flood the context for no reason (appends use `cat >> file`,
never Read). Blocks the call and tells Claude why, so it can fall back to
`wc -l` / `tail` / `grep` for anything it actually needs to inspect.
"""
import json
import sys

BLOCKED_SUFFIXES = ("data/title_review.csv",)

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)  # malformed input — don't block on our own bug

if payload.get("tool_name") != "Read":
    sys.exit(0)

file_path = (payload.get("tool_input") or {}).get("file_path", "")
if any(file_path.replace("\\", "/").endswith(suffix) for suffix in BLOCKED_SUFFIXES):
    print(
        "data/title_review.csv 禁止用 Read 整檔讀入——這個檔案只會被 append，"
        "從不需要整檔內容。要檢查用 `wc -l`、`tail -n`、`grep` 等 Bash 指令代替。",
        file=sys.stderr,
    )
    sys.exit(2)  # exit 2 = block, stderr fed back to Claude

sys.exit(0)
