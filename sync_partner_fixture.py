#!/usr/bin/env python3
"""从正在运行的 partner.py 拉取 /fixture/facts 与 /fixture/events 并回放。

这是联调适配器；中间件本身不依赖 fixture。生产环境中的合作方应直接调用
中间件的 /api/register、/api/heartbeat、/api/withdraw 与交接回执接口。
"""

from __future__ import annotations

import argparse
import json
import urllib.request

from replay_fixture import request


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--partner", default="http://127.0.0.1:8090")
    parser.add_argument("--middleware", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    partner = args.partner.rstrip("/")
    middleware = args.middleware.rstrip("/")
    facts = get_json(partner + "/fixture/facts")
    events = get_json(partner + "/fixture/events")

    status, result = request("POST", middleware + "/admin/facts", facts)
    print("facts", status, result)
    for event in events:
        status, result = request("POST", middleware + "/api/events", event)
        print(f"at={event.get('at')} {event.get('kind')} -> {status}")
        if isinstance(result, dict) and result.get("ok") is False:
            print("  ", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
