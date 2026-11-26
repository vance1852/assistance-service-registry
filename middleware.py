#!/usr/bin/env python3
"""跨机构游客求助接线公共中间件。

仅使用 Python 3.11 标准库。核心对象：

* seats/capabilities：席位地址及其登记的业务、语言、版本和在线保活；
* agreements：跨区/跨机构互助授权、新接窗口 active 与已接会话接续右端点；
* sessions：一次游客求助。会话 ID 是重连幂等键；
* handoffs：同一次交接的邀请、接手证明和陈旧回执处理。

HTTP 层很薄，业务规则都在 :class:`DispatchService` 中，便于脚本或测试直接调用。
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

DEFAULT_DB = os.environ.get(
    "DISPATCH_DB", str(Path(__file__).resolve().parent / "dispatch-state.json")
)
DEFAULT_PARTNER_BASE = os.environ.get("PARTNER_BASE", "http://127.0.0.1:8090")


def fresh_state() -> dict[str, Any]:
    return {
        "schema": 1,
        "created_at": time.time(),
        "agencies": {},
        "seats": {},
        "agreements": {},
        "sessions": {},
        "handoffs": {},
        "audit": [],
        "processed_events": {},
    }


class JsonStore:
    """进程内锁 + 原子 JSON 落盘。

    本地常驻服务使用一把可重入锁串行化状态迁移；每次迁移后原子替换文件，
    因此 kill/重启后能读到最后一次完整提交。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return fresh_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            base = fresh_state()
            base.update(data)
            return base
        except (json.JSONDecodeError, OSError):
            # 不静默覆盖可能可人工修复的文件；备份后以空状态启动。
            backup = self.path.with_suffix(self.path.suffix + f".bad-{int(time.time())}")
            try:
                self.path.rename(backup)
            except OSError:
                pass
            return fresh_state()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=False)
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def reset(self) -> None:
        with self.lock:
            self.state = fresh_state()
            self.save()


# ---------------------------------------------------------------------------
# 纯工具与规则
# ---------------------------------------------------------------------------

def current_business_minute() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute


def _pair(service: str, language: str) -> str:
    return f"{service}\u0000{language}"


def pair_key(pair: tuple[str, str] | list[str]) -> str:
    return _pair(pair[0], pair[1])


def as_pair(value: Any) -> tuple[str, str]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("替代项必须是 [service, language]")
    return str(value[0]), str(value[1])


def window_contains(window: Iterable[int] | None, at: int) -> bool:
    """active 窗口按仓库协议左闭右开。"""
    if not window or len(window) != 2:
        return False
    start, end = int(window[0]), int(window[1])
    return start <= at < end


def continuation_open(continue_until: Any, at: int) -> bool:
    """continue_until 是接续期限右端点；业务分钟粒度下包含该分钟。"""
    if continue_until is None:
        return False
    return at <= int(continue_until)


def heartbeat_covers(heartbeat: dict[str, Any] | None, at: int) -> bool:
    return bool(heartbeat and at <= int(heartbeat.get("until", -10**18)))


def public_seat(seat_id: str, seat: dict[str, Any], at: int | None = None) -> dict[str, Any]:
    at = current_business_minute() if at is None else at
    capabilities = []
    for cap in (seat.get("capabilities") or {}).values():
        hb = cap.get("heartbeat")
        capabilities.append(
            {
                "service": cap.get("service"),
                "language": cap.get("language"),
                "version": cap.get("version"),
                "withdrawn": bool(cap.get("withdrawn")),
                "registered_at": cap.get("registered_at"),
                "heartbeat": hb,
                "online_now": bool(not cap.get("withdrawn") and heartbeat_covers(hb, at)),
            }
        )
    return {
        "seat_id": seat_id,
        "id": seat_id,
        "agency": seat.get("agency"),
        "name": seat.get("name"),
        "kind": seat.get("kind"),
        "site_kind": seat.get("kind"),
        "area": seat.get("area"),
        "address": seat.get("address", seat_id),
        "handoff_url": seat.get("handoff_url"),
        "endpoint": seat.get("endpoint"),
        "capabilities": capabilities,
    }


def target_view(
    seat_id: str,
    seat: dict[str, Any],
    service: str,
    language: str,
    agreement: dict[str, Any] | None = None,
    consent_used: bool = False,
    exact_request: bool = True,
) -> dict[str, Any]:
    cap = (seat.get("capabilities") or {}).get(_pair(service, language)) or {}
    hb = cap.get("heartbeat") or {}
    delegated = bool(agreement and agreement.get("agent") != agreement.get("owner"))
    return {
        "seat_id": seat_id,
        "id": seat_id,
        "agency": seat.get("agency"),
        "name": seat.get("name"),
        "kind": seat.get("kind"),
        "area": seat.get("area"),
        "address": seat.get("address", seat_id),
        "service": service,
        "language": language,
        "exact_request": exact_request,
        "consent_used": consent_used,
        "online_until": hb.get("until"),
        "registration_version": cap.get("version"),
        "agreement_id": (agreement or {}).get("id"),
        "authorization": agreement,
        "delegated": delegated,
        "delegation_reason": (
            f"依据授权 {agreement.get('id')}：{agreement.get('owner')} "
            f"委托 {agreement.get('agent')} 在 {agreement.get('area')} "
            f"区域、{agreement.get('active')} 时段提供 {service}/{language} 互助"
            if delegated and agreement
            else None
        ),
        "invite_path": f"/seats/{seat_id}/handoffs",
    }


# ---------------------------------------------------------------------------
# 中间件业务服务
# ---------------------------------------------------------------------------

class DispatchService:
    def __init__(self, store: JsonStore, partner_base: str = DEFAULT_PARTNER_BASE):
        self.store = store
        self.partner_base = partner_base.rstrip("/")
        # 上次进程在发出邀请途中退出时，sending 不可能由本进程继续；
        # 标记为可安全重试的中断态，避免永久卡在中间状态。
        with store.lock:
            changed = False
            for handoff in store.state.get("handoffs", {}).values():
                if handoff.get("state") == "sending":
                    handoff["state"] = "send_interrupted"
                    handoff["interrupted_at_wall"] = time.time()
                    changed = True
            if changed:
                store.save()

    # ---------- 审计与幂等 ----------

    def _audit(self, state: dict[str, Any], event: str, **fields: Any) -> None:
        row = {"at_wall": time.time(), "event": event, **fields}
        state.setdefault("audit", []).append(row)
        # 本地联调状态文件不宜无限膨胀；保留最近 5000 条审计。
        if len(state["audit"]) > 5000:
            state["audit"] = state["audit"][-5000:]

    def remember_event(self, state: dict[str, Any], event: dict[str, Any], result: Any) -> bool:
        """返回 True 表示这是重复事件，调用方应直接返回缓存结果。"""
        event_id = event.get("event_id") or event.get("idempotency_key")
        if not event_id:
            return False
        key = str(event_id)
        if key in state.setdefault("processed_events", {}):
            return True
        state["processed_events"][key] = {"at_wall": time.time(), "result": result}
        return False

    # ---------- 合作方登记 / 授权 ----------

    def load_facts(self, facts: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        with self.store.lock:
            state = self.store.state
            # 允许直接传 scenario 顶层 {"facts": ...}。
            if "facts" in facts and isinstance(facts["facts"], dict):
                facts = facts["facts"]
            count = 0
            for ag in facts.get("agreements", []):
                self._upsert_agreement_locked(state, dict(ag), changed_at=at, source="facts")
                count += 1
            self._audit(state, "facts_loaded", agreements=count)
            self.store.save()
            return {"loaded": True, "agreements": count}

    def _upsert_agreement_locked(
        self, state: dict[str, Any], ag: dict[str, Any], changed_at: int | None, source: str
    ) -> dict[str, Any]:
        ag_id = str(ag["id"])
        old = state["agreements"].get(ag_id)
        if old:
            history = old.get("history", [])
            if changed_at is not None:
                before = {k: v for k, v in old.items() if k != "history"}
                history.append({"changed_at": changed_at, "before": before})
            old.update({k: v for k, v in ag.items() if k != "id"})
            old["history"] = history
            record = old
        else:
            record = dict(ag)
            record["id"] = ag_id
            record["history"] = []
            if changed_at is not None:
                record["created_at_event"] = changed_at
        record["source"] = source
        state["agreements"][ag_id] = record
        return record

    def apply_agreement_change(self, event: dict[str, Any]) -> dict[str, Any]:
        at = int(event.get("at", current_business_minute()))
        with self.store.lock:
            state = self.store.state
            ag_id = str(event.get("agreement"))
            payload = {"id": ag_id}
            for key in ("area", "owner", "agent", "service", "language", "active", "continue_until"):
                if key in event:
                    payload[key] = event[key]
            if ag_id not in state["agreements"]:
                payload.setdefault("area", event.get("area"))
                payload.setdefault("owner", event.get("owner"))
                payload.setdefault("agent", event.get("agent"))
                payload.setdefault("service", event.get("service"))
                payload.setdefault("language", event.get("language"))
            record = self._upsert_agreement_locked(state, payload, at, "agreement_change")
            self._audit(state, "agreement_changed", agreement_id=ag_id, at=at)
            self.store.save()
            return {"ok": True, "agreement": record}

    def register(self, event: dict[str, Any]) -> dict[str, Any]:
        at = int(event.get("at", current_business_minute()))
        agency = str(event["agency"])
        seat_id = str(event.get("seat") or event.get("seat_id") or event["address"])
        site_kind = event.get("site_kind") or event.get("facility_kind") or event.get("seat_type")
        if site_kind is None and event.get("kind") not in (None, "register"):
            # 直接调用登记接口时可用 kind 表示车站/景区/热线类型；
            # 回放 fixture 时 kind=register 是协议动作，不是场所类型。
            site_kind = event.get("kind")
        service = str(event["service"])
        language = str(event["language"])
        version = int(event.get("version", 1))
        key = _pair(service, language)

        with self.store.lock:
            state = self.store.state
            state["agencies"].setdefault(agency, {"id": agency})
            seat = state["seats"].setdefault(
                seat_id,
                {
                    "id": seat_id,
                    "agency": agency,
                    "address": event.get("address", seat_id),
                    "name": event.get("name"),
                    "kind": site_kind,
                    "area": event.get("area"),
                    "handoff_url": event.get("handoff_url"),
                    "endpoint": event.get("endpoint"),
                    "capabilities": {},
                },
            )
            # 允许登记资料补全地址元数据。
            for meta in (
                "address", "name", "area", "handoff_url", "endpoint",
                "invite_url", "handoff_endpoint", "base_url",
            ):
                if event.get(meta) is not None:
                    seat[meta] = event[meta]
            if site_kind is not None:
                seat["kind"] = site_kind
            if seat.get("agency") and seat["agency"] != agency:
                return {
                    "ok": False,
                    "status": 409,
                    "error": "seat_agency_conflict",
                    "message": f"席位 {seat_id} 已属于 {seat['agency']}，不能由 {agency} 登记",
                }

            cap = seat["capabilities"].get(key)
            stale = bool(cap and version < int(cap.get("version", 0)))
            if stale:
                result = {
                    "ok": False,
                    "status": 409,
                    "stale": True,
                    "error": "old_registration_version",
                    "message": f"迟到的旧登记 v{version} 不能覆盖当前 v{cap.get('version')}",
                    "current_version": cap.get("version"),
                }
                self._audit(state, "stale_registration", seat=seat_id, pair=key, version=version, at=at)
                self.store.save()
                return result

            if not cap:
                cap = {"service": service, "language": language, "heartbeat": None}
                seat["capabilities"][key] = cap
            cap.update(
                {
                    "version": version,
                    "registered_at": at,
                    "withdrawn": False,
                    "withdrawn_at": None,
                    "withdraw_version": None,
                }
            )
            self._audit(state, "registered", seat=seat_id, agency=agency, pair=key, version=version, at=at)
            self.store.save()
            return {"ok": True, "seat_id": seat_id, "service": service, "language": language, "version": version}

    def heartbeat(self, event: dict[str, Any]) -> dict[str, Any]:
        at = int(event.get("at", current_business_minute()))
        agency = str(event["agency"])
        seat_id = str(event.get("seat") or event.get("seat_id") or event["address"])
        service = str(event["service"])
        language = str(event["language"])
        version = int(event.get("version", 1))
        until = int(event["until"])
        key = _pair(service, language)

        with self.store.lock:
            state = self.store.state
            seat = state["seats"].get(seat_id)
            cap = (seat or {}).get("capabilities", {}).get(key)
            if not seat or not cap:
                result = {
                    "ok": False,
                    "status": 404,
                    "error": "capability_not_registered",
                    "message": "保活指向的业务/语言登记不存在",
                    "missing": ["registration"],
                }
                self._audit(state, "heartbeat_without_registration", seat=seat_id, pair=key, at=at)
                self.store.save()
                return result

            if cap.get("withdrawn"):
                # 核心反例：发布方撤回后，迟到保活不能恢复能力。
                result = {
                    "ok": False,
                    "status": 409,
                    "accepted": False,
                    "revived": False,
                    "error": "capability_withdrawn",
                    "message": "能力已由发布方撤回；后续保活不能恢复它",
                    "withdraw_version": cap.get("withdraw_version"),
                }
                cap.setdefault("late_heartbeats", []).append(
                    {"at": at, "version": version, "until": until, "rejected_reason": "withdrawn"}
                )
                self._audit(state, "late_heartbeat_after_withdraw", seat=seat_id, pair=key, version=version, at=at)
                self.store.save()
                return result

            current_version = int(cap.get("version", 0))
            if version < current_version:
                result = {
                    "ok": False,
                    "status": 409,
                    "accepted": False,
                    "stale": True,
                    "revived": False,
                    "error": "stale_heartbeat_version",
                    "message": f"保活版本 v{version} 旧于登记版本 v{current_version}",
                    "current_version": current_version,
                }
                cap.setdefault("late_heartbeats", []).append(
                    {"at": at, "version": version, "until": until, "rejected_reason": "stale_version"}
                )
                self._audit(state, "stale_heartbeat", seat=seat_id, pair=key, version=version, at=at)
                self.store.save()
                return result

            hb = {"version": version, "at": at, "until": until}
            cap["heartbeat"] = hb
            self._audit(state, "heartbeat", seat=seat_id, pair=key, version=version, until=until, at=at)
            self.store.save()
            return {"ok": True, "accepted": True, "seat_id": seat_id, "online_until": until}

    def withdraw(self, event: dict[str, Any]) -> dict[str, Any]:
        at = int(event.get("at", current_business_minute()))
        agency = str(event.get("agency", ""))
        seat_id = str(event.get("seat") or event.get("seat_id") or event.get("address"))
        service = str(event["service"])
        language = str(event["language"])
        version = int(event.get("version", 0))
        key = _pair(service, language)

        with self.store.lock:
            state = self.store.state
            seat = state["seats"].get(seat_id)
            cap = (seat or {}).get("capabilities", {}).get(key)
            if not seat or not cap:
                # 即使撤回先于我们见过的登记，也留下版本墓碑，防止迟到登记/保活复活。
                seat = state["seats"].setdefault(
                    seat_id,
                    {"id": seat_id, "agency": agency, "address": event.get("address", seat_id), "capabilities": {}},
                )
                cap = seat["capabilities"].setdefault(key, {"service": service, "language": language})
            if agency and seat.get("agency") and seat["agency"] != agency:
                return {"ok": False, "status": 409, "error": "seat_agency_conflict"}

            current_version = int(cap.get("version", 0) or 0)
            if version and version < current_version and cap.get("withdrawn"):
                return {"ok": True, "idempotent": True, "withdrawn": True}
            # 撤回声明的版本通常高于被撤回登记版本（如 register v3 / withdraw v4）。
            cap["withdrawn"] = True
            cap["withdrawn_at"] = at
            cap["withdraw_version"] = version or max(current_version + 1, 1)
            if version:
                cap["version"] = max(current_version, version)
            self._audit(state, "withdrawn", seat=seat_id, pair=key, version=version, at=at)
            self.store.save()
            return {
                "ok": True,
                "withdrawn": True,
                "seat_id": seat_id,
                "service": service,
                "language": language,
                "other_capabilities_unaffected": [
                    c.get("language")
                    for k, c in seat["capabilities"].items()
                    if k != key and not c.get("withdrawn")
                ],
            }

    # ---------- 候选评估 / 服务发现 ----------

    def _matching_agreements(
        self, state: dict[str, Any], area: str, service: str, language: str
    ) -> list[dict[str, Any]]:
        return [
            ag
            for ag in state.get("agreements", {}).values()
            if ag.get("service") == service
            and ag.get("language") == language
            and (
                ag.get("area") == area
                or ag.get("area") in ("*", "all")
                or area in (ag.get("areas") or [])
            )
        ]

    @staticmethod
    def agreement_terms_at(agreement: dict[str, Any], when: int) -> dict[str, Any]:
        """重建某个业务分钟实际生效的授权条款。

        agreement_change 按到达/发生分钟追加 history；若变更发生在 when 之后，
        则 when 时点应使用该次变更保存的 before 快照。
        """
        terms = dict(agreement)
        for change in reversed(agreement.get("history", [])):
            changed_at = change.get("changed_at")
            if changed_at is not None and int(changed_at) > when and change.get("before"):
                terms = dict(change["before"])
            else:
                break
        terms["id"] = agreement.get("id", terms.get("id"))
        return terms

    def evaluate_pair(
        self,
        state: dict[str, Any],
        area: str,
        service: str,
        language: str,
        at: int,
        exclude_seat: str | None = None,
        terms_at: int | None = None,
    ) -> list[dict[str, Any]]:
        """对每个已知地址评估某个业务/语言对，返回含拒绝原因的清单。"""
        results: list[dict[str, Any]] = []
        agreements = self._matching_agreements(state, area, service, language)
        if terms_at is not None:
            agreements = [self.agreement_terms_at(ag, terms_at) for ag in agreements]

        for seat_id, seat in state.get("seats", {}).items():
            reasons: list[dict[str, Any]] = []
            if exclude_seat and seat_id == exclude_seat:
                reasons.append({"code": "excluded_current_seat", "message": "交接目标不能仍是当前责任席"})

            seat_agency = seat.get("agency")
            matching_ags = [ag for ag in agreements if ag.get("agent") == seat_agency]
            if not agreements:
                reasons.append(
                    {
                        "code": "no_agreement",
                        "message": f"没有覆盖 {area}/{service}/{language} 的授权记录",
                        "missing": ["agreement"],
                    }
                )
            elif not matching_ags:
                reasons.append(
                    {
                        "code": "agency_not_authorized",
                        "message": f"机构 {seat_agency} 不是该区域 {service}/{language} 的受托/归属机构",
                        "authorized_agents": sorted({ag.get("agent") for ag in agreements}),
                    }
                )

            valid_agreement = None
            for ag in matching_ags:
                ag_reasons = []
                if not window_contains(ag.get("active"), at):
                    ag_reasons.append(
                        {
                            "code": "agreement_outside_active_window",
                            "message": f"授权 {ag.get('id')} 新接窗口 {ag.get('active')} 不包含 {at}",
                            "agreement_id": ag.get("id"),
                            "active": ag.get("active"),
                        }
                    )
                if ag_reasons:
                    reasons.extend(ag_reasons)
                elif valid_agreement is None:
                    valid_agreement = ag

            cap = (seat.get("capabilities") or {}).get(_pair(service, language))
            if not cap:
                reasons.append(
                    {
                        "code": "capability_not_registered",
                        "message": f"该地址未登记 {service}/{language} 能力",
                        "missing": ["registration"],
                    }
                )
            else:
                withdrawn_at = cap.get("withdrawn_at")
                withdrawn_at_or_before = cap.get("withdrawn") and (
                    withdrawn_at is None or int(withdrawn_at) <= at
                )
                if withdrawn_at_or_before:
                    reasons.append(
                        {
                            "code": "capability_withdrawn",
                            "message": "发布方已撤回该业务/语言能力",
                            "withdraw_version": cap.get("withdraw_version"),
                            "withdrawn_at": withdrawn_at,
                        }
                    )
                hb = cap.get("heartbeat")
                if not heartbeat_covers(hb, at):
                    reasons.append(
                        {
                            "code": "seat_offline_or_heartbeat_expired",
                            "message": "缺少覆盖当前时点的有效在线保活",
                            "heartbeat_until": (hb or {}).get("until"),
                            "missing": ["fresh_heartbeat"],
                        }
                    )
                elif int(hb.get("version", -1)) < int(cap.get("version", 0)):
                    reasons.append(
                        {
                            "code": "stale_heartbeat_version",
                            "message": "保活版本未指向当前登记版本",
                            "heartbeat_version": hb.get("version"),
                            "registration_version": cap.get("version"),
                        }
                    )

            # 授权窗口原因可能已经加入；若找到有效授权则清除同类失败中针对该授权的噪音不必要。
            eligible = not reasons and valid_agreement is not None
            item = {
                "seat_id": seat_id,
                "address": seat.get("address", seat_id),
                "agency": seat_agency,
                "kind": seat.get("kind"),
                "service": service,
                "language": language,
                "at": at,
                "eligible": eligible,
                "reasons": reasons,
            }
            if eligible:
                item["target"] = target_view(
                    seat_id, seat, service, language, valid_agreement, exact_request=False
                )
            results.append(item)
        return results

    def discover(
        self,
        payload: dict[str, Any],
        at: int | None = None,
        exclude_seat: str | None = None,
    ) -> dict[str, Any]:
        with self.store.lock:
            return self._discover_locked(payload, at, exclude_seat)

    def _discover_locked(
        self,
        payload: dict[str, Any],
        at: int | None = None,
        exclude_seat: str | None = None,
    ) -> dict[str, Any]:
        at = int(at if at is not None else payload.get("at", current_business_minute()))
        area = str(payload["area"])
        service = str(payload["service"])
        language = str(payload["language"])
        alternatives = [as_pair(a) for a in payload.get("accepted_alternatives", [])]
        allowed: list[tuple[str, str]] = [(service, language), *alternatives]

        rejected: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        selected_pair: tuple[str, str] | None = None
        used_consent = False

        for index, pair in enumerate(allowed):
            evaluations = self.evaluate_pair(
                self.store.state, area, pair[0], pair[1], at, exclude_seat=exclude_seat
            )
            for ev in evaluations:
                ev["stage"] = "discovery"
                ev["requested_pair"] = list(pair)
                ev["consented"] = index == 0 or pair in alternatives
                if not ev["eligible"]:
                    rejected.append(ev)
            candidates = [ev for ev in evaluations if ev["eligible"]]
            if candidates and selected is None:
                # 稳定排序：保活更晚、机构/席位字典序更靠前。
                candidates.sort(
                    key=lambda ev: (
                        -(ev.get("target", {}).get("online_until") or -1),
                        ev.get("agency") or "",
                        ev["seat_id"],
                    )
                )
                chosen = candidates[0]
                selected = chosen["target"]
                selected["exact_request"] = index == 0
                selected["consent_used"] = index > 0
                selected_pair = pair
                used_consent = index > 0
                # 未选中的合格地址也作为候选保留。
                selected["other_eligible"] = [c["target"] for c in candidates[1:]]
                break

        suggestions = self._unconsented_suggestions(
            self.store.state, area, service, language, allowed, at
        )

        if selected:
            status = "routable"
            message = "找到当前具备有效授权且在线的接线目标"
            if used_consent:
                message = "原请求能力不可用；按游客已同意的替代项找到接线目标"
            result = {
                "status": status,
                "assignable": True,
                "target": selected,
                "selected_target": selected,
                "selected_pair": list(selected_pair or (service, language)),
                "used_consented_alternative": used_consent,
                "alternatives": [],
                "suggestions": suggestions,
                "rejected_targets": rejected,
                "missing": [],
                "message": message,
                "at": at,
            }
        elif suggestions:
            result = {
                "status": "needs_consent",
                "assignable": False,
                "target": None,
                "selected_target": None,
                "alternatives": suggestions,
                "alternative_prompt": self._consent_prompt(suggestions[0]),
                "rejected_targets": rejected,
                "missing": [
                    f"没有当前有效的 {service}/{language} 接线能力",
                    "存在可用替代，但游客尚未明确同意该服务/语言替代",
                ],
                "message": "有可访问的替代目标，但在游客同意前不能作为退路",
                "at": at,
            }
        else:
            missing = self._summarize_missing(rejected, service, language)
            result = {
                "status": "pending_duty",
                "allocation_status": "unassignable",
                "assignable": False,
                "target": None,
                "selected_target": None,
                "alternatives": [],
                "rejected_targets": rejected,
                "missing": missing,
                "duty": {"state": "pending", "reason": "insufficient_directory_evidence", "since": at},
                "message": "目录证据不足或候选当前无效，无法自动分配；已进入待人工值守状态",
                "at": at,
            }
        return result

    def _unconsented_suggestions(
        self,
        state: dict[str, Any],
        area: str,
        service: str,
        language: str,
        allowed: list[tuple[str, str]],
        at: int,
    ) -> list[dict[str, Any]]:
        allowed_keys = {_pair(*p) for p in allowed}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ag in state.get("agreements", {}).values():
            pair = (str(ag.get("service")), str(ag.get("language")))
            if ag.get("area") != area or _pair(*pair) in allowed_keys:
                continue
            if not window_contains(ag.get("active"), at):
                continue
            seat_id = None
            for sid, seat in state.get("seats", {}).items():
                if seat.get("agency") != ag.get("agent"):
                    continue
                evals = self.evaluate_pair(state, area, pair[0], pair[1], at)
                ev = next((e for e in evals if e["seat_id"] == sid), None)
                if ev and ev.get("eligible"):
                    key = f"{sid}:{_pair(*pair)}"
                    if key not in seen:
                        seen.add(key)
                        target = ev["target"]
                        target["requires_consent"] = True
                        target["suggestion_reason"] = (
                            "同业务其他语言可用"
                            if pair[0] == service and pair[1] != language
                            else "游客可能需要另一项已授权服务"
                        )
                        out.append(target)
        out.sort(
            key=lambda t: (
                0 if t.get("service") == service else 1,
                -(t.get("online_until") or -1),
                t.get("seat_id") or "",
            )
        )
        return out

    def _consent_prompt(self, target: dict[str, Any]) -> dict[str, Any]:
        return {
            "service": target.get("service"),
            "language": target.get("language"),
            "seat_id": target.get("seat_id"),
            "text": (
                f"可由 {target.get('agency')}/{target.get('seat_id')} 提供 "
                f"{target.get('service')}/{target.get('language')}；"
                "是否征得游客明确同意，将其作为替代退路？"
            ),
        }

    def _summarize_missing(
        self, rejected: list[dict[str, Any]], service: str, language: str
    ) -> list[str]:
        codes = {r.get("code") for ev in rejected for r in ev.get("reasons", [])}
        missing: list[str] = [f"当前有效的 {service}/{language} 登记、授权和在线证据"]
        if "no_agreement" in codes:
            missing.append("覆盖请求区域、业务和语言的有效授权")
        if "capability_not_registered" in codes:
            missing.append("合作方服务/语言登记")
        if "seat_offline_or_heartbeat_expired" in codes:
            missing.append("覆盖当前时点的在线保活")
        if "capability_withdrawn" in codes:
            missing.append("发布方未撤回的能力声明")
        return missing

    # ---------- 接续条件 ----------

    def check_continuation(
        self,
        state: dict[str, Any],
        responsibility: dict[str, Any],
        at: int,
    ) -> dict[str, Any]:
        seat_id = responsibility["seat_id"]
        seat = state.get("seats", {}).get(seat_id)
        service = responsibility.get("service")
        language = responsibility.get("language")
        reasons: list[dict[str, Any]] = []
        ag = state.get("agreements", {}).get(responsibility.get("agreement_id", ""))
        if not ag:
            reasons.append({"code": "agreement_missing", "message": "找不到责任席所依据的授权"})
        else:
            if not continuation_open(ag.get("continue_until"), at):
                reasons.append(
                    {
                        "code": "continuation_period_exhausted",
                        "message": f"已接会话接续期限 {ag.get('continue_until')} 已耗尽（当前业务分钟 {at}）",
                        "continue_until": ag.get("continue_until"),
                        "at": at,
                    }
                )
            if ag.get("agent") != (seat or {}).get("agency"):
                reasons.append({"code": "agency_not_authorized", "message": "责任机构与授权受托机构不一致"})

        cap = ((seat or {}).get("capabilities") or {}).get(_pair(service, language))
        if not seat:
            reasons.append({"code": "seat_missing", "message": "责任席地址不在目录中"})
        elif not cap:
            reasons.append({"code": "capability_not_registered", "message": "责任能力登记不存在"})
        else:
            withdrawn_at = cap.get("withdrawn_at")
            if cap.get("withdrawn") and (withdrawn_at is None or int(withdrawn_at) <= at):
                reasons.append({"code": "capability_withdrawn", "message": "责任席该项能力已被发布方撤回"})
            hb = cap.get("heartbeat")
            if not heartbeat_covers(hb, at):
                reasons.append(
                    {
                        "code": "responsible_seat_unreachable",
                        "message": "责任席保活已失效，可能失联，需要安排交接或人工值守",
                        "heartbeat_until": (hb or {}).get("until"),
                    }
                )
        return {"valid": not reasons, "at": at, "reasons": reasons, "agreement": ag}

    # ---------- 前台求助 ----------

    def intake(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session") or payload.get("session_id"))
        if not session_id:
            raise ValueError("session is required")
        at = int(payload.get("at", current_business_minute()))

        with self.store.lock:
            state = self.store.state
            existing = state["sessions"].get(session_id)
            if existing:
                # 客户端重试/重连：同一会话必须幂等，不能生成第二单或第二回拨义务。
                if existing.get("intake_fingerprint") != self._intake_fingerprint(payload):
                    return {
                        "ok": False,
                        "status": 409,
                        "error": "session_conflict",
                        "message": "会话 ID 已存在，但求助要素不一致",
                        "session": self.session_view(existing, at),
                    }
                view = self._reconcile_locked(state, existing, at)
                self.store.save()
                return {"ok": True, "idempotent_replay": True, **view}

            discovery = self.discover(payload, at=at)
            session = {
                "id": session_id,
                "created_at": at,
                "last_event_at": at,
                "last_seen_at": at,
                "area": str(payload["area"]),
                "requested_service": str(payload["service"]),
                "requested_language": str(payload["language"]),
                "accepted_alternatives": payload.get("accepted_alternatives", []),
                "intake_fingerprint": self._intake_fingerprint(payload),
                "status": "new",
                "routing": None,
                "responsibility": None,
                "current_handoff_id": None,
                "handoff_ids": [],
                "routing_decisions": [],
                "handoff_summaries": [],
                "receipts": [],
                "timeline": [{"at": at, "event": "intake_created"}],
                "duty": None,
                "callback": None,
                "current_target": None,
                "alternatives": [],
                "missing": [],
            }

            decision = {
                "at": at,
                "kind": "intake",
                "result_status": discovery["status"],
                "selected": discovery.get("target"),
                "missing": discovery.get("missing"),
                "rejected_targets": discovery.get("rejected_targets", []),
                "alternatives": discovery.get("alternatives", []),
            }
            session["routing_decisions"].append(decision)

            if discovery.get("target"):
                target = discovery["target"]
                session["status"] = "awaiting_answer"
                session["current_target"] = target
                session["missing"] = []
                session["routing"] = {
                    "seat_id": target["seat_id"],
                    "agency": target.get("agency"),
                    "address": target.get("address"),
                    "service": target["service"],
                    "language": target["language"],
                    "agreement_id": target.get("agreement_id"),
                    "consent_used": target.get("consent_used", False),
                    "selected_at": at,
                    "online_until": target.get("online_until"),
                    "delegated": target.get("delegated", False),
                    "delegation_reason": target.get("delegation_reason"),
                }
                session["timeline"].append({"at": at, "event": "target_selected", "seat_id": target["seat_id"]})
            elif discovery["status"] == "needs_consent":
                session["status"] = "needs_consent"
                session["alternatives"] = discovery.get("alternatives", [])
                session["missing"] = discovery.get("missing", [])
                session["alternative_prompt"] = discovery.get("alternative_prompt")
            else:
                session["status"] = "pending_duty"
                session["alternatives"] = []
                session["missing"] = discovery.get("missing", [])
                session["duty"] = discovery.get("duty")
                session["duty"]["since"] = at

            state["sessions"][session_id] = session
            self._audit(state, "intake", session=session_id, at=at, status=session["status"])
            self.store.save()
            return {"ok": True, **self.session_view(session, at, discovery=discovery)}

    @staticmethod
    def _intake_fingerprint(payload: dict[str, Any]) -> str:
        parts = [
            str(payload.get("area")),
            str(payload.get("service")),
            str(payload.get("language")),
            json.dumps(payload.get("accepted_alternatives", []), sort_keys=True, ensure_ascii=False),
        ]
        return "|".join(parts)

    def answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session") or payload.get("session_id"))
        seat_id = str(payload.get("seat") or payload.get("seat_id"))
        at = int(payload.get("at", current_business_minute()))
        with self.store.lock:
            state = self.store.state
            session = state["sessions"].get(session_id)
            if not session:
                return {"ok": False, "status": 404, "error": "session_not_found"}

            routing = session.get("routing") or {}
            if routing.get("seat_id") and routing["seat_id"] != seat_id:
                # 也允许该席在回答时点本身具备资格，但必须留下改派原因。
                check = self.evaluate_pair(
                    state,
                    session["area"],
                    routing.get("service", session["requested_service"]),
                    routing.get("language", session["requested_language"]),
                    at,
                )
                if not any(e["seat_id"] == seat_id and e["eligible"] for e in check):
                    result = {
                        "ok": False,
                        "status": 409,
                        "error": "answer_seat_mismatch",
                        "message": "回报接起的席位不是当前有效路由目标",
                        "routed_target": routing.get("seat_id"),
                    }
                    session["timeline"].append({"at": at, "event": "answer_rejected", "seat_id": seat_id})
                    self.store.save()
                    return result

            pair_service = routing.get("service", session["requested_service"])
            pair_language = routing.get("language", session["requested_language"])
            evals = self.evaluate_pair(state, session["area"], pair_service, pair_language, at)
            ev = next((e for e in evals if e["seat_id"] == seat_id), None)
            if not ev or not ev["eligible"]:
                session["timeline"].append({"at": at, "event": "answer_rejected", "seat_id": seat_id, "evaluation": ev})
                self._reconcile_locked(state, session, at)
                self.store.save()
                return {
                    "ok": False,
                    "status": 422,
                    "error": "seat_not_currently_eligible",
                    "message": "接起回报时点该席不具备所需当前能力",
                    "evaluation": ev,
                    "session": self.session_view(session, at),
                }

            agreement_id = (ev.get("target") or {}).get("agreement_id")
            session["status"] = "active"
            session["last_seen_at"] = at
            session["responsibility"] = {
                "seat_id": seat_id,
                "agency": (state["seats"].get(seat_id) or {}).get("agency"),
                "service": pair_service,
                "language": pair_language,
                "agreement_id": agreement_id,
                "since": at,
                "answer_at": at,
                "source": "answer",
            }
            session["original_responsibility"] = dict(session["responsibility"])
            session["callback"] = {
                "obligation_id": f"callback-{session_id}",
                "state": "pending",
                "owner_seat": seat_id,
                "owner_agency": (state["seats"].get(seat_id) or {}).get("agency"),
                "single_open_obligation": True,
                "revision": 1,
                "prior_owners": [],
            }
            session["timeline"].append({"at": at, "event": "answered", "seat_id": seat_id})
            self._audit(state, "answer", session=session_id, seat=seat_id, at=at)
            self.store.save()
            return {"ok": True, **self.session_view(session, at)}

    def reconnect(self, session_id: str, at: int | None = None) -> dict[str, Any]:
        at = int(at if at is not None else current_business_minute())
        with self.store.lock:
            state = self.store.state
            session = state["sessions"].get(session_id)
            if not session:
                return {
                    "ok": False,
                    "status": 404,
                    "error": "session_not_found",
                    "message": "未找到该会话；前台应按新求助发起 intake",
                }
            view = self._reconcile_locked(state, session, at)
            self.store.save()
            return {"ok": True, "reconnected": True, **view}

    def _reconcile_locked(self, state: dict[str, Any], session: dict[str, Any], at: int) -> dict[str, Any]:
        session["last_seen_at"] = at
        responsibility = session.get("responsibility")
        if responsibility:
            check = self.check_continuation(state, responsibility, at)
            session["continuation"] = check
            if check["valid"]:
                if session.get("status") != "closed":
                    session["status"] = "active"
                target = target_view(
                    responsibility["seat_id"],
                    state["seats"].get(responsibility["seat_id"], {}),
                    responsibility["service"],
                    responsibility["language"],
                    state["agreements"].get(responsibility.get("agreement_id", "")),
                    exact_request=False,
                )
                session["current_target"] = target
            else:
                was_pending_duty = session.get("status") == "pending_duty"
                session["status"] = "pending_duty"
                duty = session.get("duty") or {}
                claimed = duty.get("state") == "claimed"
                duty.update(
                    {
                        "reason": "continuation_unavailable",
                        "since": duty.get("since", at),
                        "observed_at": at,
                        "reason_codes": [r["code"] for r in check["reasons"]],
                        "reasons": check["reasons"],
                    }
                )
                # 已有人认领值守后，重复重连/管理端刷新不能把认领状态冲回 pending。
                duty["state"] = "claimed" if claimed else "pending"
                session["duty"] = duty
                session["current_target"] = None
                cb = session.get("callback")
                if cb and cb.get("state") != "claimed_by_duty":
                    cb["state"] = "held_for_duty"
                    cb["duty_queue"] = True
                    cb["exclusive_owner"] = "duty_pool"
                if not was_pending_duty and not any(e.get("event") == "entered_duty" for e in session["timeline"]):
                    session["timeline"].append(
                        {"at": at, "event": "entered_duty", "reasons": check["reasons"]}
                    )
                self._audit(state, "session_duty_pending", session=session["id"], at=at)
        elif session.get("routing") and session.get("status") == "awaiting_answer":
            routing = session["routing"]
            evals = self.evaluate_pair(
                state,
                session["area"],
                routing["service"],
                routing["language"],
                at,
            )
            ev = next((e for e in evals if e["seat_id"] == routing["seat_id"]), None)
            session["routing"]["currently_eligible"] = bool(ev and ev["eligible"])
            if ev and ev["eligible"]:
                session["status"] = "awaiting_answer"
                session["current_target"] = ev["target"]
            else:
                # 尚未接起，原责任尚未形成；重新发现可用退路。
                discovery = self.discover(
                    {
                        "area": session["area"],
                        "service": session["requested_service"],
                        "language": session["requested_language"],
                        "accepted_alternatives": session.get("accepted_alternatives", []),
                    },
                    at=at,
                )
                session["refresh"] = discovery
                if discovery.get("target"):
                    session["current_target"] = discovery["target"]
                    session["status"] = "reroutable_before_answer"
                elif discovery["status"] == "needs_consent":
                    session["status"] = "needs_consent"
                    session["alternative_prompt"] = discovery.get("alternative_prompt")
                    session["current_target"] = None
                else:
                    session["status"] = "pending_duty"
                    session["duty"] = discovery.get("duty")
                    session["current_target"] = None
        return self.session_view(session, at)

    # ---------- 交接邀请 ----------

    def prepare_handoff(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session") or payload.get("session_id"))
        handoff_id = str(payload.get("handoff_id") or payload.get("id") or f"h-{uuid.uuid4().hex[:12]}")
        at = int(payload.get("at", current_business_minute()))
        explicit_seat = payload.get("seat") or payload.get("target_seat") or payload.get("to_seat")
        reason = payload.get("reason") or "front_desk_requested_transfer"

        with self.store.lock:
            state = self.store.state
            existing = state["handoffs"].get(handoff_id)
            if existing:
                # 同一个 prepared 记录只允许一个调用者真正发邀请，避免并发重试
                # 让合作方收到两次同 ID 邀请。崩溃恢复后的显式 retry 可重发。
                can_claim = existing.get("state") == "prepared" or (
                    payload.get("retry")
                    and existing.get("state") in ("invite_failed", "send_interrupted")
                )
                if can_claim:
                    existing["state"] = "sending"
                    self.store.save()
                    return {
                        "ok": True,
                        "idempotent": True,
                        "send": True,
                        "handoff_id": handoff_id,
                        "url": self._invite_url(existing["to_seat"]),
                        "body": existing["invite_request"],
                        "handoff": handoff_view(existing),
                    }
                return {
                    "ok": True,
                    "idempotent": True,
                    "send": False,
                    "status": 202 if existing.get("state") in ("sending", "invited", "accepted") else 200,
                    "handoff": handoff_view(existing),
                }

            session = state["sessions"].get(session_id)
            if not session:
                return {"ok": False, "status": 404, "error": "session_not_found"}

            from_seat = ((session.get("responsibility") or {}).get("seat_id")) or (
                session.get("routing") or {}
            ).get("seat_id")
            requirement = {
                "area": session["area"],
                "service": session["requested_service"],
                "language": session["requested_language"],
                "accepted_alternatives": session.get("accepted_alternatives", []),
            }
            discovery = self.discover(requirement, at=at, exclude_seat=None if explicit_seat else from_seat)

            target = None
            target_pair = None
            rejections = discovery.get("rejected_targets", [])
            if explicit_seat:
                # 管理员/前台指定席也要过当前授权与在线检查。
                pair_candidates = [(session["requested_service"], session["requested_language"])]
                pair_candidates += [as_pair(a) for a in session.get("accepted_alternatives", [])]
                for pair in pair_candidates:
                    evals = self.evaluate_pair(state, session["area"], pair[0], pair[1], at)
                    ev = next((e for e in evals if e["seat_id"] == explicit_seat), None)
                    if ev:
                        if ev["eligible"]:
                            target = ev["target"]
                            target_pair = pair
                            break
                        rejections.append(ev)
                if target is None:
                    discovery["status"] = "no_valid_explicit_target"
            elif discovery.get("target"):
                target = discovery["target"]
                target_pair = discovery.get("selected_pair")

            handoff = {
                "id": handoff_id,
                "session_id": session_id,
                "at": at,
                "reason": reason,
                "from_seat": from_seat,
                "to_seat": target["seat_id"] if target else explicit_seat,
                "selected_pair": list(target_pair) if target_pair else None,
                "state": "prepared" if target else "no_candidate",
                "invite_request": {"session": session_id, "handoff_id": handoff_id},
                "invite_url": self._invite_url(target["seat_id"]) if target else None,
                "partner_response": None,
                "proof": None,
                "receipts": [],
                "rejected_targets": rejections,
                "created_at_wall": time.time(),
            }
            state["handoffs"][handoff_id] = handoff
            session.setdefault("handoff_ids", []).append(handoff_id)
            session["timeline"].append(
                {"at": at, "event": "handoff_prepared", "handoff_id": handoff_id, "to_seat": handoff["to_seat"]}
            )

            if not target:
                session["timeline"].append({"at": at, "event": "handoff_no_candidate", "handoff_id": handoff_id})
                self._audit(state, "handoff_no_candidate", handoff=handoff_id, session=session_id, at=at)
                self.store.save()
                return {
                    "ok": False,
                    "status": 422,
                    "error": "no_eligible_handoff_target",
                    "handoff_id": handoff_id,
                    "handoff": handoff_view(handoff),
                    "discovery": discovery,
                }

            handoff["state"] = "sending"
            session["current_handoff_id"] = handoff_id
            self._audit(state, "handoff_prepared", handoff=handoff_id, session=session_id, to=target["seat_id"], at=at)
            self.store.save()
            return {"ok": True, "send": True, "handoff_id": handoff_id, "url": handoff["invite_url"], "body": handoff["invite_request"], "handoff": handoff_view(handoff)}

    def _invite_url(self, seat_id: str) -> str:
        with self.store.lock:
            seat = self.store.state.get("seats", {}).get(seat_id, {})
        # 明确命名为 handoff/invite URL 的字段视为完整地址。
        for key in ("handoff_url", "handoff_endpoint", "invite_url"):
            explicit = seat.get(key)
            if explicit:
                return str(explicit)

        # endpoint 或可访问 address 可以是席位资源根，补 /handoffs。
        base_address = seat.get("endpoint") or seat.get("address") or seat_id
        base_address = str(base_address).rstrip("/")
        if base_address.startswith(("http://", "https://")):
            if base_address.endswith("/handoffs"):
                return base_address
            return base_address + "/handoffs"

        # fixture/联调资料端用席位标识匹配 /seats/{id}/handoffs。
        return f"{self.partner_base}/seats/{base_address}/handoffs"

    def send_prepared_handoff(self, prepared: dict[str, Any]) -> dict[str, Any]:
        if not prepared.get("send"):
            return prepared
        url = prepared["url"]
        body = json.dumps(prepared["body"], ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
        )
        http_info: dict[str, Any]
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    parsed = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                http_info = {"ok": 200 <= resp.status < 300, "status": resp.status, "response": parsed}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            http_info = {"ok": False, "status": exc.code, "response": parsed, "error": str(exc)}
        except Exception as exc:  # URLError, RemoteDisconnected, timeout etc.
            http_info = {"ok": False, "error": type(exc).__name__, "message": str(exc)}

        handoff_id = prepared["handoff_id"]
        with self.store.lock:
            state = self.store.state
            handoff = state["handoffs"][handoff_id]
            handoff["partner_response"] = {**http_info, "at_wall": time.time()}
            if http_info.get("ok") and http_info.get("status") == 202:
                handoff["state"] = "invited"
                handoff["invited_at"] = handoff.get("at")
                event_name = "handoff_invited"
            else:
                handoff["state"] = "invite_failed"
                event_name = "handoff_invite_failed"
            session = state["sessions"][handoff["session_id"]]
            session["timeline"].append(
                {"at": handoff.get("at"), "event": event_name, "handoff_id": handoff_id, "http": http_info}
            )
            self._audit(state, event_name, handoff=handoff_id, http=http_info.get("status") or http_info.get("error"))
            self.store.save()
            return {"ok": http_info.get("ok", False), "handoff": handoff_view(handoff), "http": http_info}

    def create_and_send_handoff(self, payload: dict[str, Any]) -> dict[str, Any]:
        prepared = self.prepare_handoff(payload)
        if not prepared.get("send"):
            return prepared
        sent = self.send_prepared_handoff(prepared)
        status_code = 202 if sent.get("ok") else 502
        if sent.get("ok"):
            return {"ok": True, "status": 202, **sent}
        return {"ok": False, "status": status_code, "error": "partner_invite_failed", **sent}

    # ---------- 接手回执 / 证明 ----------

    def accept_handoff(self, handoff_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        at = int(payload.get("at", current_business_minute()))
        accepted_at = int(payload.get("accepted_at", payload.get("at", at)))
        seat_id = str(payload.get("seat") or payload.get("seat_id"))

        with self.store.lock:
            state = self.store.state
            handoff = state["handoffs"].get(handoff_id)
            if not handoff:
                return {"ok": False, "status": 404, "error": "handoff_not_found"}

            session_id = handoff["session_id"]
            session = state["sessions"].get(session_id)
            receipt = {
                "at": at,
                "accepted_at": accepted_at,
                "seat_id": seat_id,
                "payload": {k: v for k, v in payload.items() if k != "at"},
            }

            # 幂等重放同一张已接受证明；同一次交接的不同回执不能覆盖证明。
            if handoff.get("state") == "accepted" and handoff.get("proof"):
                proof = handoff["proof"]
                if proof.get("seat_id") == seat_id and int(proof.get("accepted_at")) == accepted_at:
                    receipt["disposition"] = "duplicate_proof"
                    handoff.setdefault("receipts", []).append(receipt)
                    self.store.save()
                    return {"ok": True, "idempotent_replay": True, "handoff": handoff_view(handoff), "proof": proof}
                receipt["disposition"] = "conflict_rejected"
                receipt["reasons"] = [{"code": "conflicting_acceptance_proof", "message": "该交接已有不同的接手证明"}]
                handoff.setdefault("receipts", []).append(receipt)
                self.store.save()
                return {"ok": False, "status": 409, "error": "handoff_already_accepted", "proof": proof}

            if session is None:
                return {"ok": False, "status": 409, "error": "session_missing_for_handoff"}

            already_transferred = bool(
                session.get("responsibility")
                and session["responsibility"].get("handoff_id")
                and session["responsibility"]["handoff_id"] != handoff_id
            )
            if already_transferred:
                current_id = session["responsibility"].get("handoff_id")
                receipt["disposition"] = "stale_rejected"
                receipt["reasons"] = [
                    {
                        "code": "stale_handoff_receipt",
                        "message": "该回执属于旧交接；会话已由另一次交接转出，旧回执不能抢回责任",
                        "current_handoff_id": current_id,
                    }
                ]
                handoff.setdefault("receipts", []).append(receipt)
                handoff["state"] = "stale_rejected"
                session["timeline"].append(
                    {"at": at, "event": "stale_acceptance_rejected", "handoff_id": handoff_id, "seat_id": seat_id}
                )
                self._audit(state, "stale_acceptance_rejected", handoff=handoff_id, seat=seat_id)
                self.store.save()
                return {
                    "ok": False,
                    "status": 409,
                    "error": "stale_handoff_receipt",
                    "message": receipt["reasons"][0]["message"],
                    "current_responsibility": session.get("responsibility"),
                }

            if handoff.get("to_seat") and seat_id != handoff["to_seat"]:
                reasons = [
                    {
                        "code": "acceptor_not_invited_target",
                        "message": "回报接手的席位不是本次交接邀请的目标",
                        "invited_target": handoff.get("to_seat"),
                        "reported_seat": seat_id,
                    }
                ]
                receipt["disposition"] = "rejected"
                receipt["reasons"] = reasons
                handoff.setdefault("receipts", []).append(receipt)
                handoff["state"] = "acceptance_rejected"
                self.store.save()
                return {"ok": False, "status": 409, "error": "seat_mismatch", "reasons": reasons}

            pair = handoff.get("selected_pair")
            if not pair:
                # 若邀请时未选定（例如外部建单），从会话替代项中找该席当前合格 pair。
                pair = self._eligible_pair_for_seat(state, session, seat_id, accepted_at)
                if not pair:
                    reasons = [{"code": "no_matching_authorized_pair", "message": "找不到该席在接受时点有效的业务/语言授权"}]
                    receipt.update({"disposition": "rejected", "reasons": reasons})
                    handoff.setdefault("receipts", []).append(receipt)
                    self.store.save()
                    return {"ok": False, "status": 422, "error": "invalid_acceptance", "reasons": reasons}

            accept_evals = self.evaluate_pair(
                state, session["area"], pair[0], pair[1], accepted_at, terms_at=accepted_at
            )
            accept_ev = next((e for e in accept_evals if e["seat_id"] == seat_id), None)
            accepted_valid = bool(accept_ev and accept_ev["eligible"])
            accept_reasons = [] if accepted_valid else (accept_ev or {}).get("reasons", [
                {"code": "target_not_found", "message": "接手席不在目录中"}
            ])

            provisional_resp = {
                "seat_id": seat_id,
                "service": pair[0],
                "language": pair[1],
                "agreement_id": ((accept_ev or {}).get("target") or {}).get("agreement_id"),
            }
            continuation = self.check_continuation(state, provisional_resp, at)
            current_valid = continuation["valid"]

            # 交接完成前，这通求助仍在原责任席的已接会话接续期限内。
            # 若该期限在回执处理时已耗尽，则不能再靠迟到回执完成转出，应进值守。
            transfer_window_reasons: list[dict[str, Any]] = []
            prior_responsibility = session.get("responsibility") or {}
            prior_agreement = state.get("agreements", {}).get(prior_responsibility.get("agreement_id", ""))
            if prior_agreement and not continuation_open(prior_agreement.get("continue_until"), at):
                transfer_window_reasons.append(
                    {
                        "code": "prior_continuation_period_exhausted",
                        "message": "原责任席的接续期限在接手回执处理前已耗尽，不能再标记成功转出",
                        "prior_agreement_id": prior_agreement.get("id"),
                        "continue_until": prior_agreement.get("continue_until"),
                        "at": at,
                    }
                )
            transfer_window_valid = not transfer_window_reasons

            if not accepted_valid or not current_valid or not transfer_window_valid:
                reasons = []
                if not accepted_valid:
                    reasons.append(
                        {
                            "code": "not_authorized_at_acceptance_time",
                            "message": f"接受发生的业务分钟 {accepted_at} 不具备有效候选资格",
                            "details": accept_reasons,
                        }
                    )
                if not current_valid:
                    reasons.append(
                        {
                            "code": "continuation_conditions_not_met_at_receipt",
                            "message": f"回执到达/处理时点 {at} 的当前接续条件不满足",
                            "details": continuation.get("reasons", []),
                        }
                    )
                if not transfer_window_valid:
                    reasons.append(
                        {
                            "code": "transfer_window_exhausted",
                            "message": "原席接续期限已耗尽且此前未取得接手证明",
                            "details": transfer_window_reasons,
                        }
                    )
                receipt.update({"disposition": "rejected", "reasons": reasons})
                handoff.setdefault("receipts", []).append(receipt)
                handoff["state"] = "acceptance_rejected"
                session["timeline"].append(
                    {"at": at, "event": "acceptance_rejected", "handoff_id": handoff_id, "reasons": reasons}
                )
                # 若失败原因是原接续期限已尽，立即把会话落到待值守状态。
                self._reconcile_locked(state, session, at)
                self._audit(state, "acceptance_rejected", handoff=handoff_id, reasons=[r["code"] for r in reasons])
                self.store.save()
                return {"ok": False, "status": 422, "error": "acceptance_not_effective", "reasons": reasons}

            agreement_id = provisional_resp["agreement_id"]
            proof = {
                "proof_id": f"proof-{handoff_id}",
                "kind": "seat_acceptance_proof",
                "handoff_id": handoff_id,
                "session_id": session_id,
                "seat_id": seat_id,
                "agency": (state["seats"].get(seat_id) or {}).get("agency"),
                "accepted_at": accepted_at,
                "received_at": at,
                "service": pair[0],
                "language": pair[1],
                "agreement_id": agreement_id,
                "acceptance_time_check": {
                    "valid": True,
                    "at": accepted_at,
                    "agreement_id": agreement_id,
                    "active": ((accept_ev or {}).get("target") or {}).get("authorization", {}).get("active"),
                },
                "current_continuation_check": {
                    "valid": True,
                    "at": at,
                    "continue_until": (continuation.get("agreement") or {}).get("continue_until"),
                },
                "prior_transfer_window_check": {
                    "valid": transfer_window_valid,
                    "at": at,
                    "prior_agreement_id": (prior_agreement or {}).get("id"),
                    "continue_until": (prior_agreement or {}).get("continue_until"),
                },
                "inbound_report": {"handoff_id": handoff_id, "seat": seat_id, "accepted_at": accepted_at},
            }
            handoff["state"] = "accepted"
            handoff["accepted_at"] = accepted_at
            handoff["proof"] = proof
            handoff.setdefault("receipts", []).append({**receipt, "disposition": "accepted"})

            old_responsibility = session.get("responsibility")
            old_callback = session.get("callback") or {}
            if old_callback:
                old_callback.setdefault("prior_owners", []).append(
                    {
                        "seat_id": old_callback.get("owner_seat"),
                        "revision": old_callback.get("revision"),
                        "ended_at": at,
                        "ended_by_handoff": handoff_id,
                        "resolution": "transferred_single_obligation",
                    }
                )
                old_callback["owner_seat"] = seat_id
                old_callback["owner_agency"] = proof["agency"]
                old_callback["state"] = "pending"
                old_callback["revision"] = int(old_callback.get("revision", 1)) + 1
                old_callback["duty_queue"] = False
                old_callback["exclusive_owner"] = None
                session["callback"] = old_callback
            else:
                session["callback"] = {
                    "obligation_id": f"callback-{session_id}",
                    "state": "pending",
                    "owner_seat": seat_id,
                    "owner_agency": proof["agency"],
                    "single_open_obligation": True,
                    "revision": 1,
                    "prior_owners": [],
                }

            session["responsibility"] = {
                "seat_id": seat_id,
                "agency": proof["agency"],
                "service": pair[0],
                "language": pair[1],
                "agreement_id": agreement_id,
                "since": accepted_at,
                "responsibility_transferred_at": at,
                "handoff_id": handoff_id,
                "proof_id": proof["proof_id"],
            }
            session["current_handoff_id"] = handoff_id
            session["status"] = "active"
            session["last_seen_at"] = at
            session["duty"] = None
            session["timeline"].append(
                {"at": at, "event": "responsibility_transferred", "handoff_id": handoff_id, "to_seat": seat_id, "proof_id": proof["proof_id"]}
            )
            self._audit(state, "handoff_accepted", handoff=handoff_id, session=session_id, seat=seat_id)
            self.store.save()
            return {"ok": True, "status": 200, "handoff": handoff_view(handoff), "proof": proof, "responsibility": session["responsibility"]}

    def _eligible_pair_for_seat(
        self, state: dict[str, Any], session: dict[str, Any], seat_id: str, at: int
    ) -> tuple[str, str] | None:
        pairs = [(session["requested_service"], session["requested_language"])]
        pairs.extend(as_pair(a) for a in session.get("accepted_alternatives", []))
        resp = session.get("responsibility") or {}
        if resp.get("service"):
            pairs.append((resp["service"], resp["language"]))
        for pair in dict.fromkeys(pairs):
            evals = self.evaluate_pair(state, session["area"], pair[0], pair[1], at)
            ev = next((e for e in evals if e["seat_id"] == seat_id and e["eligible"]), None)
            if ev:
                return pair
        return None

    # ---------- 人工值守 ----------

    def claim_duty(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        at = int(payload.get("at", current_business_minute()))
        duty_id = str(payload.get("duty_id") or payload.get("officer") or "duty-officer")
        with self.store.lock:
            state = self.store.state
            session = state["sessions"].get(session_id)
            if not session:
                return {"ok": False, "status": 404, "error": "session_not_found"}
            duty = session.get("duty") or {"state": "pending"}
            duty.update({"state": "claimed", "claimed_by": duty_id, "claimed_at": at})
            session["duty"] = duty
            cb = session.get("callback")
            if cb:
                cb["state"] = "claimed_by_duty"
                cb["owner_seat"] = f"duty:{duty_id}"
                cb["duty_queue"] = False
            session["timeline"].append({"at": at, "event": "duty_claimed", "by": duty_id})
            self._audit(state, "duty_claimed", session=session_id, by=duty_id)
            self.store.save()
            return {"ok": True, "session": self.admin_session_view(session_id)}

    # ---------- 视图 ----------

    def session_view(self, session: dict[str, Any], at: int | None = None, discovery: dict | None = None) -> dict[str, Any]:
        at = at if at is not None else session.get("last_seen_at") or session.get("last_event_at") or current_business_minute()
        resp = session.get("responsibility")
        target = session.get("current_target")
        if resp and session.get("status") == "active":
            # 责任已经因接手证明转移时，视图必须以当前责任席为准，
            # 不能继续返回 intake 时缓存的旧目标。
            state = self.store.state
            target = target_view(
                resp.get("seat_id"),
                state.get("seats", {}).get(resp.get("seat_id"), {}),
                resp.get("service"),
                resp.get("language"),
                state.get("agreements", {}).get(resp.get("agreement_id", "")),
                exact_request=False,
            )
        elif target is None and session.get("status") == "awaiting_answer" and session.get("routing"):
            target = session.get("routing")
        status = session.get("status")
        assignable = status in ("awaiting_answer", "active", "reroutable_before_answer") and target is not None
        return {
            "session_id": session["id"],
            "id": session["id"],
            "status": status,
            "assignable": assignable,
            "at": at,
            "area": session.get("area"),
            "requested": {"service": session.get("requested_service"), "language": session.get("requested_language")},
            "target": target,
            "selected_target": target,
            "routing": session.get("routing"),
            "used_consented_alternative": bool((session.get("routing") or {}).get("consent_used")),
            "responsibility": resp,
            "original_responsibility": session.get("original_responsibility"),
            "current_handoff_id": session.get("current_handoff_id"),
            "handoff_ids": session.get("handoff_ids", []),
            "callback": session.get("callback"),
            "duty": session.get("duty"),
            "continuation": session.get("continuation"),
            "alternatives": session.get("alternatives", []),
            "missing": session.get("missing", []),
            "alternative_prompt": session.get("alternative_prompt"),
            "refresh": session.get("refresh"),
            "message": self._status_message(session),
        }

    @staticmethod
    def _status_message(session: dict[str, Any]) -> str:
        status = session.get("status")
        return {
            "awaiting_answer": "已找到当前有效目标，等待席位接起",
            "active": "求助已接起，重连按原会话和当前责任席识别",
            "needs_consent": "有替代目标，但需游客明确同意",
            "pending_duty": "暂时不能自动分配或接续，已进入待人工值守状态",
            "reroutable_before_answer": "原路由失效；接起前可改派到新的有效目标",
        }.get(status, status or "")

    def admin_session_view(self, session_id: str, at: int | None = None) -> dict[str, Any] | None:
        with self.store.lock:
            session = self.store.state["sessions"].get(session_id)
            if not session:
                return None
            if at is not None:
                self._reconcile_locked(self.store.state, session, int(at))
                self.store.save()
            rejected: list[dict[str, Any]] = []
            for decision in session.get("routing_decisions", []):
                for ev in decision.get("rejected_targets", []):
                    rejected.append({"stage": decision.get("kind"), "at": decision.get("at"), **ev})
            for hid in session.get("handoff_ids", []):
                h = self.store.state["handoffs"].get(hid, {})
                for ev in h.get("rejected_targets", []):
                    rejected.append({"stage": "handoff", "handoff_id": hid, "at": h.get("at"), **ev})
            handoffs = [
                handoff_view(self.store.state["handoffs"][hid])
                for hid in session.get("handoff_ids", [])
                if hid in self.store.state["handoffs"]
            ]
            return {
                **self.session_view(session, at),
                "timeline": session.get("timeline", []),
                "routing_decisions": session.get("routing_decisions", []),
                "handoffs": handoffs,
                "proofs": [h.get("proof") for h in handoffs if h.get("proof")],
                "rejected_targets": rejected,
                "rejections": rejected,
                "receipts": [r for h in handoffs for r in h.get("receipts", [])],
                "intake": {
                    "area": session.get("area"),
                    "service": session.get("requested_service"),
                    "language": session.get("requested_language"),
                    "accepted_alternatives": session.get("accepted_alternatives"),
                },
            }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.store.lock:
            return [self.session_view(s) for s in self.store.state.get("sessions", {}).values()]

    def duty_queue(self) -> list[dict[str, Any]]:
        with self.store.lock:
            return [
                self.admin_session_view(sid)
                for sid, s in self.store.state.get("sessions", {}).items()
                if s.get("status") == "pending_duty" or (s.get("duty") or {}).get("state") == "pending"
            ]

    def directory(self, at: int | None = None) -> dict[str, Any]:
        at = current_business_minute() if at is None else int(at)
        with self.store.lock:
            state = self.store.state
            return {
                "at": at,
                "seats": {sid: public_seat(sid, seat, at) for sid, seat in state.get("seats", {}).items()},
                "agreements": state.get("agreements", {}),
                "agencies": state.get("agencies", {}),
            }

    def generic_event(self, event: dict[str, Any]) -> dict[str, Any]:
        kind = event.get("kind")
        # 简单事件 ID 幂等；fixture 无 event_id 时各处理函数本身也尽量幂等。
        with self.store.lock:
            if event.get("event_id") and self.remember_event(self.store.state, event, {"accepted": True}):
                cached = self.store.state["processed_events"][str(event["event_id"])]
                return {"ok": True, "idempotent_replay": True, **cached.get("result", {})}

        if kind == "register":
            return self.register(event)
        if kind == "heartbeat":
            return self.heartbeat(event)
        if kind == "withdraw":
            return self.withdraw(event)
        if kind == "agreement_change":
            return self.apply_agreement_change(event)
        if kind == "intake":
            return self.intake(event)
        if kind == "answer":
            return self.answer(event)
        if kind == "invite":
            return self.create_and_send_handoff(event)
        if kind == "accept":
            return self.accept_handoff(str(event["handoff_id"]), event)
        if kind == "reconnect":
            return self.reconnect(str(event["session"]), int(event["at"]) if event.get("at") is not None else None)
        if kind in ("offline", "seat_lost"):
            return self.mark_offline(event)
        return {"ok": False, "status": 400, "error": "unknown_event_kind", "kind": kind}

    def mark_offline(self, event: dict[str, Any]) -> dict[str, Any]:
        seat_id = str(event.get("seat") or event.get("seat_id"))
        at = int(event.get("at", current_business_minute()))
        service = event.get("service")
        language = event.get("language")
        with self.store.lock:
            state = self.store.state
            seat = state["seats"].get(seat_id)
            if not seat:
                return {"ok": False, "status": 404, "error": "seat_not_found"}
            if service and language:
                cap = seat["capabilities"].get(_pair(str(service), str(language)))
                if cap:
                    cap["heartbeat"] = {"version": cap.get("version"), "at": at, "until": at - 1, "offline": True}
            else:
                for cap in seat.get("capabilities", {}).values():
                    cap["heartbeat"] = {"version": cap.get("version"), "at": at, "until": at - 1, "offline": True}
            self._audit(state, "seat_marked_offline", seat=seat_id, at=at)
            self.store.save()
            return {"ok": True, "seat_id": seat_id}


def handoff_view(h: dict[str, Any]) -> dict[str, Any]:
    return {
        "handoff_id": h.get("id"),
        "id": h.get("id"),
        "session_id": h.get("session_id"),
        "state": h.get("state"),
        "at": h.get("at"),
        "reason": h.get("reason"),
        "from_seat": h.get("from_seat"),
        "to_seat": h.get("to_seat"),
        "selected_pair": h.get("selected_pair"),
        "invite_url": h.get("invite_url"),
        "partner_response": h.get("partner_response"),
        "proof": h.get("proof"),
        "receipts": h.get("receipts", []),
        "rejected_targets": h.get("rejected_targets", []),
    }


# ---------------------------------------------------------------------------
# HTTP 适配
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DispatchMiddleware/1.0"

    @property
    def service(self) -> DispatchService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("DISPATCH_HTTP_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_result(self, result: Any, default: int = 200) -> None:
        code = default
        if isinstance(result, dict):
            status = result.get("status")
            if isinstance(status, int) and not isinstance(status, bool):
                code = status
            elif result.get("ok") is False and default < 400:
                code = 400
        self._send(code, result)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.end_headers()

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        if self.headers.get("Idempotency-Key") and not data.get("event_id"):
            data["event_id"] = self.headers["Idempotency-Key"]
        return data

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            at = int(query["at"]) if query.get("at") is not None else None
            self.route_get(path, query, at)
        except KeyError as exc:
            self._send(400, {"ok": False, "error": "missing_field", "field": str(exc)})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": "bad_request", "message": str(exc)})
        except Exception as exc:
            self._send(500, {"ok": False, "error": "internal_error", "message": str(exc)})

    def do_POST(self) -> None:
        try:
            payload = self._body()
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            self.route_post(path, payload)
        except json.JSONDecodeError as exc:
            self._send(400, {"ok": False, "error": "invalid_json", "message": str(exc)})
        except KeyError as exc:
            self._send(400, {"ok": False, "error": "missing_field", "field": str(exc)})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": "bad_request", "message": str(exc)})
        except Exception as exc:
            self._send(500, {"ok": False, "error": "internal_error", "message": str(exc)})

    do_PUT = do_POST

    def route_get(self, path: str, query: dict[str, str], at: int | None) -> None:
        svc = self.service
        if path == "/":
            self._send(200, {**API_INDEX, "ok": True, "time": current_business_minute()})
            return
        if path in ("/healthz", "/api/health"):
            self._send(200, {"ok": True, "service": "dispatch-middleware", "time": current_business_minute()})
            return
        if path in ("/api", "/api/index"):
            self._send(200, API_INDEX)
            return
        if path in ("/api/directory", "/directory", "/api/addresses", "/addresses"):
            self._send(200, svc.directory(at))
            return
        if path in ("/api/sessions", "/sessions", "/admin/sessions", "/api/calls", "/calls", "/admin/calls"):
            self._send(200, {"sessions": svc.list_sessions(), "calls": svc.list_sessions()})
            return
        if path in ("/api/continue", "/continue", "/api/front-desk/continue"):
            sid = query.get("session") or query.get("session_id")
            if not sid:
                self._send(400, {"ok": False, "error": "missing_field", "field": "session"})
            else:
                result = svc.reconnect(sid, at)
                self._send(404 if result.get("status") == 404 else 200, result)
            return
        if path in ("/admin/duty", "/api/duty", "/duty"):
            self._send(200, {"queue": svc.duty_queue()})
            return
        if path in ("/admin/state", "/api/state"):
            with svc.store.lock:
                self._send(200, svc.store.state)
            return
        if path in ("/admin/audit", "/api/audit"):
            with svc.store.lock:
                self._send(200, {"audit": svc.store.state.get("audit", [])})
            return

        parts = [p for p in path.split("/") if p]
        # /api/discovery?area=...
        if path in ("/api/discovery", "/discover", "/discovery"):
            payload = {
                "area": query.get("area"),
                "service": query.get("service"),
                "language": query.get("language"),
                "accepted_alternatives": json.loads(query.get("accepted_alternatives", "[]")),
            }
            if at is not None:
                payload["at"] = at
            self._send(200, svc.discover(payload, at=at))
            return

        # /api/sessions/{id} and /api|admin/calls/{id} aliases
        if len(parts) >= 3 and parts[0] in ("api", "admin") and parts[1] in ("sessions", "calls", "front-desk"):
            # /api/front-desk/sessions/{id}
            if parts[1] == "front-desk" and len(parts) >= 4 and parts[2] == "sessions":
                sid = parts[3]
                if len(parts) >= 5 and parts[4] == "reconnect":
                    result = svc.reconnect(sid, at)
                    self._send(int(result.pop("status", 200)) if "status" in result and isinstance(result["status"], int) else 200, result)
                    return
                result = svc.reconnect(sid, at)
                self._send(404 if result.get("status") == 404 else 200, result)
                return
            if parts[1] in ("sessions", "calls"):
                sid = parts[2]
                if len(parts) == 3:
                    if parts[0] == "admin":
                        view = svc.admin_session_view(sid, at)
                        if not view:
                            self._send(404, {"ok": False, "error": "session_not_found"})
                        else:
                            self._send(200, view)
                        return
                    result = svc.reconnect(sid, at)
                    self._send(404 if result.get("status") == 404 else 200, result)
                    return
                if len(parts) == 4 and parts[3] == "reconnect":
                    result = svc.reconnect(sid, at)
                    self._send(404 if result.get("status") == 404 else 200, result)
                    return
                if len(parts) == 4 and parts[3] in ("rejections", "rejected-targets"):
                    view = svc.admin_session_view(sid)
                    self._send(200, {"rejected_targets": (view or {}).get("rejected_targets", [])})
                    return
                if len(parts) == 4 and parts[3] == "timeline":
                    view = svc.admin_session_view(sid)
                    self._send(200, {"timeline": (view or {}).get("timeline", [])})
                    return

        if len(parts) >= 3 and parts[0] in ("api", "admin") and parts[1] == "handoffs" and len(parts) == 3:
            with svc.store.lock:
                h = svc.store.state["handoffs"].get(parts[2])
            if not h:
                self._send(404, {"ok": False, "error": "handoff_not_found"})
            else:
                self._send(200, handoff_view(h))
            return

        self._send(404, {"ok": False, "error": "not_found", "path": path})

    def route_post(self, path: str, payload: dict[str, Any]) -> None:
        svc = self.service

        if path in ("/api/events", "/events", "/api/v1/events"):
            if isinstance(payload.get("events"), list):
                results = [svc.generic_event(ev) for ev in payload["events"]]
                status = 207 if any(r.get("ok") is False for r in results) else 200
                self._send(status, {"results": results})
                return
            result = svc.generic_event(payload)
            self._send_result(result)
            return

        if path in ("/admin/facts", "/api/facts", "/facts"):
            self._send(200, svc.load_facts(payload))
            return

        if path in (
            "/api/registry/registrations",
            "/registrations",
            "/register",
            "/api/register",
        ):
            result = svc.register(payload)
            self._send_result(result)
            return
        if path in ("/api/registry/heartbeats", "/heartbeats", "/heartbeat", "/api/heartbeat"):
            result = svc.heartbeat(payload)
            self._send_result(result)
            return
        if path in ("/api/registry/withdrawals", "/withdraw", "/withdrawals", "/api/withdraw"):
            result = svc.withdraw(payload)
            self._send_result(result)
            return
        if path in ("/api/agreements", "/agreements"):
            result = svc.load_facts({"agreements": payload.get("agreements", [payload])})
            self._send(200, result)
            return
        if path in ("/api/agreement-changes", "/agreement_change"):
            self._send(200, svc.apply_agreement_change(payload))
            return

        if path in ("/api/discovery", "/discover", "/discovery"):
            at = int(payload["at"]) if payload.get("at") is not None else None
            self._send(200, svc.discover(payload, at=at))
            return

        if path in (
            "/api/intakes", "/intake", "/intakes",
            "/api/front-desk/intakes", "/api/sessions",
            "/api/calls", "/calls", "/api/help/new", "/help/new",
        ):
            result = svc.intake(payload)
            self._send_result(result)
            return

        if path in (
            "/api/front-desk/reconnect", "/api/continue", "/continue",
            "/reconnect", "/api/help/resume", "/help/resume",
        ):
            sid = str(payload.get("session") or payload.get("session_id"))
            at = int(payload["at"]) if payload.get("at") is not None else None
            result = svc.reconnect(sid, at)
            self._send_result(result, 404 if result.get("status") == 404 else 200)
            return

        if path in (
            "/api/handoffs", "/handoffs", "/api/handoff", "/handoff",
            "/api/front-desk/handoffs", "/api/transfer", "/transfer",
        ):
            result = svc.create_and_send_handoff(payload)
            self._send_result(result, 202)
            return
        if path in ("/accept", "/api/accept", "/api/handoffs/accept"):
            hid = str(payload.get("handoff_id") or payload.get("id"))
            result = svc.accept_handoff(hid, payload)
            self._send_result(result)
            return

        parts = [p for p in path.split("/") if p]
        # /api/sessions/{id}/answer|reconnect|handoffs|duty/claim
        if len(parts) >= 3 and parts[0] == "api" and parts[1] in ("sessions", "calls"):
            sid = parts[2]
            if len(parts) == 4 and parts[3] == "answer":
                payload["session"] = sid
                result = svc.answer(payload)
                self._send_result(result)
                return
            if len(parts) == 4 and parts[3] == "reconnect":
                at = int(payload["at"]) if payload.get("at") is not None else None
                result = svc.reconnect(sid, at)
                self._send_result(result, 404 if result.get("status") == 404 else 200)
                return
            if len(parts) == 4 and parts[3] == "handoffs":
                payload["session"] = sid
                result = svc.create_and_send_handoff(payload)
                self._send_result(result, 202)
                return
            if len(parts) == 5 and parts[3] == "duty" and parts[4] == "claim":
                result = svc.claim_duty(sid, payload)
                self._send_result(result)
                return

        # /api/handoffs|handoff/{id}/accept(ance)
        if (
            len(parts) >= 4 and parts[0] == "api"
            and parts[1] in ("handoffs", "handoff")
            and parts[3] in ("accept", "acceptance", "receipt")
        ):
            result = svc.accept_handoff(parts[2], payload)
            self._send_result(result)
            return

        # Seat-facing aliases:
        # POST /seats/{seat}/handoffs/{handoff} or .../{accept|acceptance}
        if len(parts) >= 4 and parts[0] == "seats" and parts[2] == "handoffs":
            if len(parts) == 4:
                payload.setdefault("seat", parts[1])
                result = svc.accept_handoff(parts[3], payload)
                self._send_result(result)
                return
            if len(parts) == 5 and parts[4] in ("accept", "acceptance", "receipt"):
                payload.setdefault("seat", parts[1])
                result = svc.accept_handoff(parts[3], payload)
                self._send_result(result)
                return

        # /admin/sessions|calls/{id}/duty/claim
        if (
            len(parts) == 5 and parts[0] == "admin"
            and parts[1] in ("sessions", "calls")
            and parts[3] == "duty" and parts[4] == "claim"
        ):
            result = svc.claim_duty(parts[2], payload)
            self._send_result(result)
            return

        self._send(404, {"ok": False, "error": "not_found", "path": path})


API_INDEX = {
    "name": "dispatch-middleware",
    "endpoints": [
        "GET /healthz",
        "POST /api/events  (body may be one fixture event or {events:[...]})",
        "POST /admin/facts",
        "POST /api/register | /api/heartbeat | /api/withdraw",
        "POST /api/discovery",
        "POST /api/intakes",
        "GET|POST /api/sessions/{id}",
        "POST /api/sessions/{id}/answer",
        "POST /api/sessions/{id}/handoffs",
        "POST /api/handoffs/{handoff_id}/acceptance",
        "GET /admin/sessions/{id}",
        "GET /admin/sessions/{id}/rejections",
        "GET /admin/duty",
        "POST /admin/sessions/{id}/duty/claim",
        "GET /admin/state",
    ],
}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: DispatchService):
        super().__init__(address, Handler)
        self.service = service


def build_service(db_path: str | Path, partner_base: str) -> DispatchService:
    return DispatchService(JsonStore(db_path), partner_base=partner_base)


def main() -> None:
    parser = argparse.ArgumentParser(description="跨机构游客求助接线公共中间件")
    parser.add_argument("--host", default=os.environ.get("DISPATCH_HOST", os.environ.get("HOST", "127.0.0.1")))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT") or os.environ.get("DISPATCH_PORT", "8080")),
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="持久化 JSON 状态文件")
    parser.add_argument("--partner-base", default=DEFAULT_PARTNER_BASE, help="合作方资料/席位接口基址")
    parser.add_argument("--facts", type=Path, help="启动时载入的 fixture 或 facts JSON")
    args = parser.parse_args()

    service = build_service(args.db, args.partner_base)
    if args.facts:
        data = json.loads(args.facts.read_text(encoding="utf-8"))
        service.load_facts(data)

    httpd = Server((args.host, args.port), service)
    print(f"接线中间件 http://{args.host}:{args.port}", flush=True)
    print(f"状态文件 {Path(args.db).resolve()}", flush=True)
    print(f"合作方基址 {args.partner_base}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
