"""DB/MQ/Trace 只读观测网关端到端与等待/报告测试。"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
from core import compile as C
from core import runner as R
from core import report as Report


class Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/ok":
                    self.send_response(200); self.end_headers(); return
                if parsed.path != "/query":
                    self.send_error(404); return
                body = json.loads(json.dumps(parent.responses[0]))
                if len(parent.responses) > 1:
                    parent.responses.pop(0)
                query = parse_qs(parsed.query)
                parent.queries.append(query)
                if (not isinstance(body.get("correlation"), dict)
                        or not all(body["correlation"].get(key) for key in ("run_id", "case_id", "step_id"))):
                    body["correlation"] = {key: query.get(key, [""])[0]
                                           for key in ("run_id", "case_id", "step_id")}
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d" % self.server.server_port

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


def answer(records, **extra):
    out = {"read_only": True, "truncated": False, "source": "orders", "kind": "DB",
           "correlation": {"run_id": "", "case_id": "", "step_id": ""}, "records": records}
    out.update(extra)
    return out


def spec(gateway_url, wait=None, records=None):
    return {
        "name": "观测端到端", "base_url": gateway_url, "auth": {"type": "none"},
        "autotest": {"mode": "isolated", "allow_hosts": ["127.0.0.1"],
                     "allow_observation_gateway": True},
        "observability": {"gateway": {"base_url": gateway_url, "timeout_ms": 500},
                            "sources": {"orders": {"kind": "DB"}}},
        "cases": [{"id": "TC-OBS-001", "模块": "订单", "用例标题": "订单落库",
                   "steps": [{"名称": "提交订单", "method": "GET", "path": "/ok",
                              **({"观测等待": wait} if wait else {}),
                              "断言": [{"类型": "状态码", "等于": 200},
                                       {"类型": "DB", "来源": "orders", "数量": records if records is not None else {"大于等于": 1}}]}]}],
    }


class TestObservabilityCompile(unittest.TestCase):
    def test_拒绝后端直连和命令式来源(self):
        bad = spec("http://127.0.0.1:9000")
        bad["observability"]["gateway"]["sql"] = "select * from orders"
        bad["observability"]["sources"]["orders"]["traceql"] = "{ .service = \"x\" }"
        errors = [m for level, m in C.lint_spec(bad) if level == "error"]
        self.assertTrue(any("SQL/MQ 命令/TraceQL" in m for m in errors))
        self.assertTrue(any("预声明" in m for m in errors))

    def test_来源类型和等待字段在lint期校验(self):
        bad = spec("http://127.0.0.1:9000", {"来源": "missing", "直到": {"类型": "DB", "来源": "missing", "数量": 1}, "超时": "x"})
        bad["cases"][0]["steps"][0]["断言"][1]["类型"] = "MQ"
        errors = [m for level, m in C.lint_spec(bad) if level == "error"]
        self.assertTrue(any("未在 observability.sources 声明" in m for m in errors))
        self.assertTrue(any("必须是非负数字" in m for m in errors))
        self.assertTrue(any("类型是 DB" in m for m in errors))


class TestObservabilityRunner(unittest.TestCase):
    def _fixed(self, records, **extra):
        body = answer(records, **extra)
        body["correlation"] = None
        return body

    def test_真实本地HTTP网关等待后通过并记录脱敏证据(self):
        first = self._fixed([])
        second = self._fixed([{"id": "O-1", "token": "secret-token"}])
        gateway = Gateway([first, second, second, second])
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "spec.json")
                wait = {"来源": "orders", "直到": {"类型": "DB", "来源": "orders", "数量": {"大于等于": 1}}, "超时": 300, "间隔": 1}
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(spec(gateway.base_url, wait), f)
                result = R.run_spec(path, out_dir=os.path.join(temp, "out"), quiet=True)
                self.assertEqual("passed", result["cases"][0]["status"], result["cases"][0]["reason"])
                step = result["cases"][0]["steps"][0]
                self.assertEqual(2, step["observation_wait"]["次数"])
                evidence = step["observability"]["orders"]["summary"]
                self.assertEqual("[REDACTED]", evidence["records_sample"][0]["token"])
                self.assertNotIn("_records", step["observability"]["orders"])
                self.assertEqual(["orders"], gateway.queries[0]["source"])
                for query in gateway.queries:
                    self.assertTrue(query["run_id"][0])
                    self.assertEqual("TC-OBS-001", query["case_id"][0])
                    self.assertTrue(query["step_id"][0])
        finally:
            gateway.stop()

    def test_网关非只读稳定归类为blocked(self):
        gateway = Gateway([answer([], read_only=False)])
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "spec.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(spec(gateway.base_url), f)
                result = R.run_spec(path, out_dir=os.path.join(temp, "out"), quiet=True)
                self.assertEqual("blocked", result["cases"][0]["status"])
                self.assertIn("read_only", result["cases"][0]["reason"])
        finally:
            gateway.stop()

    def test_等待条件超时是failed而非规格错误(self):
        gateway = Gateway([answer([])])
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "spec.json")
                wait = {"来源": "orders", "直到": {"类型": "DB", "来源": "orders", "数量": 1}, "超时": 0, "间隔": 1}
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(spec(gateway.base_url, wait, 0), f)
                result = R.run_spec(path, out_dir=os.path.join(temp, "out"), quiet=True)
                self.assertEqual("failed", result["cases"][0]["status"])
                self.assertIn("观测等待超时", result["cases"][0]["reason"])
        finally:
            gateway.stop()


class TestObservabilityReport(unittest.TestCase):
    def test_报告展示观测等待与安全摘要(self):
        base = {"summary": {"auto": 1, "passed": 0, "failed": 1, "blocked": 0, "skipped": 0,
                            "manual": 0, "flaky": 0, "assertions_passed": 0, "assertions_total": 1, "total": 1},
                "target": {"base": "http://h", "env": "test", "auth_type": "none", "credential_ok": True},
                "run": {"spec_name": "观测", "spec_version": "1", "started_at": "2026-10-27", "duration_ms": 1, "id": "r", "spec_path": "s.json"},
                "cases": [{"id": "TC-OBS", "module": "M", "title": "t", "priority": "P1", "status": "failed", "flaky": False, "duration_ms": 1, "reason": "观测等待超时", "basis": "", "defect_level": "", "assertions": {"passed": 0, "total": 1}, "steps": [{"name": "s", "method": "GET", "url": "http://h/s", "status": 200, "elapsed_ms": 1, "passed": False, "ui": False, "response_excerpt": "", "observation_wait": {"来源": "orders", "次数": 2, "超时": True}, "observability": {"orders": {"summary": {"record_count": 0, "records_sample": [{"token": "[REDACTED]"}]}}}}]}],
                "defects": [{"case": "TC-OBS", "module": "M", "level": "未分级", "title": "t", "reason": "观测等待超时"}]}
        text = Report.render_md(base)
        self.assertIn("观测等待", text)
        self.assertIn("[REDACTED]", text)


if __name__ == "__main__":
    unittest.main()
