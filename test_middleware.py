"""接线中间件核心规则与持久化恢复测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from middleware import DispatchService, JsonStore, Server
from partner import PartnerServer, load_scenario


ROOT = Path(__file__).parent


class MiddlewareRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "dispatch.json"
        self.service = DispatchService(JsonStore(self.db), partner_base="http://127.0.0.1:9")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def agreement(self, **overrides):
        ag = {
            "id": "a1",
            "area": "central",
            "owner": "city",
            "agent": "hub",
            "service": "lost",
            "language": "zh",
            "active": [100, 300],
            "continue_until": 360,
        }
        ag.update(overrides)
        self.service.load_facts({"agreements": [ag]})
        return ag

    def online_seat(self, seat="s1", agency="hub", service="lost", language="zh", at=120, until=240, version=1):
        self.service.register(
            {
                "at": at,
                "kind": "register",
                "agency": agency,
                "seat": seat,
                "service": service,
                "language": language,
                "version": version,
                "site_kind": "station",
                "address": f"addr-{seat}",
            }
        )
        self.service.heartbeat(
            {
                "at": at,
                "kind": "heartbeat",
                "agency": agency,
                "seat": seat,
                "service": service,
                "language": language,
                "version": version,
                "until": until,
            }
        )

    def intake_and_answer(self, session="case-1", seat="s1", at=120, language="zh", alternatives=None):
        intake = self.service.intake(
            {
                "at": at,
                "session": session,
                "area": "central",
                "service": "lost",
                "language": language,
                "accepted_alternatives": alternatives or [],
            }
        )
        self.assertTrue(intake.get("target"), intake)
        answer = self.service.answer({"at": at + 1, "session": session, "seat": seat})
        self.assertTrue(answer.get("ok"), answer)
        return answer

    def test_no_evidence_reports_missing_and_duty(self):
        result = self.service.intake(
            {
                "at": 100,
                "session": "empty",
                "area": "nowhere",
                "service": "lost",
                "language": "zh",
                "accepted_alternatives": [],
            }
        )
        self.assertEqual(result["status"], "pending_duty")
        self.assertFalse(result["assignable"])
        self.assertTrue(result["missing"])
        self.assertEqual(result["duty"]["state"], "pending")

    def test_unconsented_available_address_is_only_prompt(self):
        self.agreement(id="a-sign", language="zh")
        self.online_seat()
        result = self.service.intake(
            {
                "at": 120,
                "session": "sign-no-consent",
                "area": "central",
                "service": "lost",
                "language": "sign",
                "accepted_alternatives": [],
            }
        )
        self.assertEqual(result["status"], "needs_consent")
        self.assertIsNone(result["target"])
        self.assertEqual(result["alternatives"][0]["language"], "zh")
        self.assertIn("同意", result["alternative_prompt"]["text"])

    def test_consented_alternative_is_used(self):
        self.agreement(id="a-sign", language="zh")
        self.online_seat()
        result = self.service.intake(
            {
                "at": 120,
                "session": "sign-yes",
                "area": "central",
                "service": "lost",
                "language": "sign",
                "accepted_alternatives": [["lost", "zh"]],
            }
        )
        self.assertEqual(result["status"], "awaiting_answer")
        self.assertEqual(result["target"]["seat_id"], "s1")
        self.assertTrue(result["used_consented_alternative"])

    def test_reachable_seat_without_authorization_is_rejected_with_reason(self):
        self.agreement(agent="authorized-agency")
        self.online_seat(seat="reachable", agency="other-agency")
        discovery = self.service.discover(
            {"area": "central", "service": "lost", "language": "zh"}, at=120
        )
        self.assertIsNone(discovery["target"])
        ev = discovery["rejected_targets"][0]
        self.assertEqual(ev["seat_id"], "reachable")
        self.assertIn("agency_not_authorized", [r["code"] for r in ev["reasons"]])

    def test_withdraw_blocks_late_heartbeat_and_preserves_other_language(self):
        self.agreement(id="a-yue", language="yue")
        self.agreement(id="a-zh", language="zh")
        self.online_seat(language="yue", version=3, until=150)
        self.online_seat(language="zh", version=6, until=200)

        withdrawn = self.service.withdraw(
            {"at": 160, "agency": "hub", "seat": "s1", "service": "lost", "language": "yue", "version": 4}
        )
        self.assertTrue(withdrawn["ok"])
        late = self.service.heartbeat(
            {
                "at": 161,
                "agency": "hub",
                "seat": "s1",
                "service": "lost",
                "language": "yue",
                "version": 3,
                "until": 300,
            }
        )
        self.assertFalse(late["ok"])
        self.assertEqual(late["error"], "capability_withdrawn")

        yue = self.service.discover(
            {"area": "central", "service": "lost", "language": "yue"}, at=162
        )
        zh = self.service.discover(
            {"area": "central", "service": "lost", "language": "zh"}, at=162
        )
        self.assertIsNone(yue["target"])
        self.assertEqual(zh["target"]["seat_id"], "s1")

    def test_new_intake_blocked_after_active_window_but_existing_continues(self):
        self.agreement(active=[100, 200], continue_until=220)
        self.online_seat(at=120, until=260)
        self.intake_and_answer(at=120)

        new_case = self.service.intake(
            {"at": 205, "session": "new-after-window", "area": "central", "service": "lost", "language": "zh"}
        )
        self.assertEqual(new_case["status"], "pending_duty")

        reconnect = self.service.reconnect("case-1", at=210)
        self.assertEqual(reconnect["status"], "active")
        expired = self.service.reconnect("case-1", at=221)
        self.assertEqual(expired["status"], "pending_duty")
        self.assertIn(
            "continuation_period_exhausted",
            [r["code"] for r in expired["duty"]["reasons"]],
        )
        claimed = self.service.claim_duty("case-1", {"duty_id": "officer-7", "at": 222})
        self.assertTrue(claimed["ok"], claimed)
        refreshed = self.service.admin_session_view("case-1", at=225)
        self.assertEqual(refreshed["duty"]["state"], "claimed")
        self.assertEqual(refreshed["duty"]["claimed_by"], "officer-7")

    def test_handoff_requires_acceptance_proof(self):
        self.agreement()
        self.agreement(id="a2", agent="hub2")
        self.online_seat(seat="old", agency="hub")
        self.online_seat(seat="new", agency="hub2")
        self.intake_and_answer(seat="old")

        prepared = self.service.prepare_handoff(
            {"session": "case-1", "handoff_id": "h1", "at": 130, "seat": "new"}
        )
        self.assertTrue(prepared["ok"], prepared)
        admin = self.service.admin_session_view("case-1")
        self.assertEqual(admin["responsibility"]["seat_id"], "old")
        self.assertIsNone(admin["handoffs"][0]["proof"])

        accepted = self.service.accept_handoff(
            "h1", {"at": 140, "accepted_at": 135, "seat": "new"}
        )
        self.assertTrue(accepted["ok"], accepted)
        admin = self.service.admin_session_view("case-1")
        self.assertEqual(admin["responsibility"]["seat_id"], "new")
        self.assertEqual(admin["proofs"][0]["handoff_id"], "h1")

    def test_stale_old_receipt_cannot_reclaim_or_create_second_callback(self):
        self.agreement()
        self.agreement(id="a2", agent="hub2")
        self.agreement(id="a3", agent="hub3")
        self.online_seat(seat="old", agency="hub")
        self.online_seat(seat="n1", agency="hub2")
        self.online_seat(seat="n2", agency="hub3")
        self.intake_and_answer(seat="old")

        self.service.prepare_handoff({"session": "case-1", "handoff_id": "old-h", "at": 130, "seat": "n1"})
        self.service.prepare_handoff({"session": "case-1", "handoff_id": "new-h", "at": 131, "seat": "n2"})
        accepted = self.service.accept_handoff(
            "new-h", {"at": 140, "accepted_at": 132, "seat": "n2"}
        )
        self.assertTrue(accepted["ok"], accepted)

        stale = self.service.accept_handoff(
            "old-h", {"at": 141, "accepted_at": 133, "seat": "n1"}
        )
        self.assertEqual(stale["status"], 409)
        self.assertEqual(stale["error"], "stale_handoff_receipt")

        admin = self.service.admin_session_view("case-1")
        self.assertEqual(admin["responsibility"]["seat_id"], "n2")
        self.assertEqual(admin["responsibility"]["handoff_id"], "new-h")
        cb = admin["callback"]
        self.assertEqual(cb["obligation_id"], "callback-case-1")
        self.assertTrue(cb["single_open_obligation"])
        self.assertEqual(cb["owner_seat"], "n2")
        self.assertEqual([p["seat_id"] for p in cb["prior_owners"]], ["old"])
        self.assertNotIn("n1", [cb["owner_seat"], *[p["seat_id"] for p in cb["prior_owners"]]])

    def test_acceptance_checked_at_accept_time_and_current_continuation(self):
        self.agreement(active=[100, 200], continue_until=210)
        self.online_seat(until=260)
        self.intake_and_answer()
        self.service.prepare_handoff({"session": "case-1", "handoff_id": "h-time", "at": 120, "seat": "s1"})

        early = self.service.accept_handoff(
            "h-time", {"at": 130, "accepted_at": 90, "seat": "s1"}
        )
        self.assertEqual(early["status"], 422)
        self.assertIn("not_authorized_at_acceptance_time", [r["code"] for r in early["reasons"]])

        late_report = self.service.accept_handoff(
            "h-time", {"at": 211, "accepted_at": 150, "seat": "s1"}
        )
        self.assertEqual(late_report["status"], 422)
        self.assertIn(
            "continuation_conditions_not_met_at_receipt",
            [r["code"] for r in late_report["reasons"]],
        )
        admin = self.service.admin_session_view("case-1")
        self.assertEqual(admin["responsibility"]["seat_id"], "s1")
        self.assertIsNone(admin["handoffs"][0]["proof"])

        ok = self.service.accept_handoff(
            "h-time", {"at": 205, "accepted_at": 150, "seat": "s1"}
        )
        self.assertTrue(ok["ok"], ok)

    def test_restart_recovers_session_proof_and_rejections(self):
        self.agreement()
        self.online_seat()
        self.intake_and_answer()
        before = self.service.reconnect("case-1", at=130)
        self.assertEqual(before["status"], "active")

        restarted = DispatchService(JsonStore(self.db), partner_base="http://127.0.0.1:9")
        after = restarted.reconnect("case-1", at=131)
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["responsibility"]["seat_id"], "s1")
        admin = restarted.admin_session_view("case-1")
        self.assertTrue(admin["timeline"])
        self.assertIn("answered", [e["event"] for e in admin["timeline"]])


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = DispatchService(
            JsonStore(Path(self.tmp.name) / "http.json"), partner_base="http://127.0.0.1:9"
        )
        self.server = Server(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            return exc.code, json.loads(raw)

    def test_rest_routes_do_not_confuse_session_status_with_http_status(self):
        status, _ = self.call("POST", "/admin/facts", {"agreements": [{
            "id": "a1", "area": "central", "owner": "city", "agent": "hub",
            "service": "lost", "language": "zh", "active": [0, 1440], "continue_until": 1440,
        }]})
        self.assertEqual(status, 200)
        for event in [
            {"kind": "register", "at": 100, "agency": "hub", "seat": "s1",
             "service": "lost", "language": "zh", "version": 1},
            {"kind": "heartbeat", "at": 100, "agency": "hub", "seat": "s1",
             "service": "lost", "language": "zh", "version": 1, "until": 1400},
        ]:
            status, body = self.call("POST", "/api/events", event)
            self.assertEqual(status, 200, body)

        status, intake = self.call("POST", "/api/intakes", {
            "at": 110, "session": "http-1", "area": "central",
            "service": "lost", "language": "zh",
        })
        self.assertEqual(status, 200, intake)
        self.assertTrue(intake["target"])

        status, answer = self.call("POST", "/api/sessions/http-1/answer", {"at": 111, "seat": "s1"})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["status"], "active")

        status, admin = self.call("GET", "/admin/sessions/http-1")
        self.assertEqual(status, 200)
        self.assertEqual(admin["responsibility"]["seat_id"], "s1")
        self.assertIn("rejected_targets", admin)

        status, again = self.call("POST", "/api/continue", {"session": "http-1", "at": 112})
        self.assertEqual(status, 200, again)
        self.assertEqual(again["session_id"], "http-1")


class FixtureIntegrationTest(unittest.TestCase):
    def start_partner(self, path: Path):
        scenario = load_scenario(path)
        self.partner = PartnerServer(("127.0.0.1", 0), scenario)
        self.thread = threading.Thread(target=self.partner.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.partner.shutdown)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.partner.server_close)
        self.base = f"http://127.0.0.1:{self.partner.server_port}"

    def test_night_delegation_protocol(self):
        fixture = ROOT / "fixtures" / "night-delegation.json"
        self.start_partner(fixture)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = DispatchService(
            JsonStore(Path(tmp.name) / "night.json"), partner_base=self.base
        )
        scenario = json.loads(fixture.read_text(encoding="utf-8"))
        service.load_facts(scenario["facts"])

        results = []
        for event in scenario["events"]:
            results.append(service.generic_event(event))

        case = service.admin_session_view("case-8")
        self.assertEqual(case["routing"]["seat_id"], "h1")
        self.assertTrue(case["routing"]["delegated"])
        self.assertEqual(case["responsibility"]["seat_id"], "r4")
        self.assertEqual(case["proofs"][0]["handoff_id"], "h-26")
        self.assertEqual(case["proofs"][0]["accepted_at"], 1261)
        self.assertEqual(case["status"], "pending_duty")
        self.assertEqual(case["duty"]["reason_codes"], ["continuation_period_exhausted"])
        self.assertTrue(case["rejected_targets"])
        # 合作方只应收到仓库协议规定的那一次邀请。
        self.assertEqual(len(self.partner.journal), 1)
        self.assertTrue(self.partner.journal[0]["matched"])
        self.assertEqual(self.partner.journal[0]["body"], {"session": "case-8", "handoff_id": "h-26"})

    def test_language_consent_protocol(self):
        fixture = ROOT / "fixtures" / "language-consent.json"
        scenario = json.loads(fixture.read_text(encoding="utf-8"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = DispatchService(JsonStore(Path(tmp.name) / "lang.json"))
        service.load_facts(scenario["facts"])
        for event in scenario["events"]:
            service.generic_event(event)

        no_consent = service.admin_session_view("case-17")
        consented = service.admin_session_view("case-18")
        self.assertEqual(no_consent["status"], "needs_consent")
        self.assertEqual(no_consent["alternatives"][0]["seat_id"], "a2")
        self.assertEqual(consented["status"], "active")
        self.assertEqual(consented["responsibility"]["seat_id"], "a2")


if __name__ == "__main__":
    unittest.main()
