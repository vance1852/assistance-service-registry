#!/usr/bin/env python3
"""按 fixture 中 events 的业务时间顺序回放给接线中间件。

合作方资料端 partner.py 需另行启动；invite 事件会由中间件向
``--partner-base`` 指定的席位地址发送 POST /seats/{id}/handoffs。
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def request(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--middleware", default="http://127.0.0.1:8080")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    scenario = json.loads(args.fixture.read_text(encoding="utf-8"))
    base = args.middleware.rstrip("/")
    status, facts = request("POST", base + "/admin/facts", scenario.get("facts", {}))
    print("facts", status, facts)

    last_session = None
    for event in scenario.get("events", []):
        kind = event["kind"]
        status, result = request("POST", base + "/api/events", event)
        if event.get("session"):
            last_session = event["session"]
        if not args.quiet:
            brief = result
            if isinstance(result, dict):
                # 完整 rejected_targets 很长；事件流中只打印关键状态。
                brief = {
                    k: result.get(k)
                    for k in (
                        "ok",
                        "status",
                        "error",
                        "message",
                        "state",
                        "idempotent_replay",
                    )
                    if k in result
                }
                if "handoff" in result:
                    brief["handoff_state"] = result["handoff"].get("state")
                if "target" in result and result["target"]:
                    brief["target"] = result["target"].get("seat_id")
                if "responsibility" in result and result["responsibility"]:
                    brief["responsible"] = result["responsibility"].get("seat_id")
            print(f"at={event.get('at')} {kind} -> HTTP {status} {json.dumps(brief, ensure_ascii=False)}")

    if last_session:
        status, admin = request("GET", f"{base}/admin/sessions/{last_session}")
        print("\n=== admin session", last_session, "HTTP", status, "===")
        print(json.dumps(admin, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
