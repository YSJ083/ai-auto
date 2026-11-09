import json
import os
import sys
import tempfile
import unittest
from urllib.error import URLError
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
from core import asserts as A
from core import virtual as V
from core import runner as R

class TestConcurrentAssert(unittest.TestCase):
    def test_同一订单收敛为一个ID(self):
        rows = [{"status": 201, "json": {"data": {"id": "O1"}}} for _ in range(3)]
        ok, _ = A.judge({"类型":"并发结果","请求数":3,"成功数":3,"状态码":{"全部在":[201]},"JSON字段":{"路径":"data.id","去重数":1,"非空":True}}, {"concurrency":{"responses":rows}}, {})
        self.assertTrue(ok)

class TestConcurrentRunner(unittest.TestCase):
    def test_真实并发请求共享幂等键并携带不可覆盖关联头(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                with lock:
                    seen.append({key.lower(): value for key, value in self.headers.items()})
                payload = b'{"data":{"id":"ORDER-1"}}'
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        spec = {
            "name": "并发端到端", "base_url": base, "auth": {"type": "none"},
            "autotest": {"mode": "isolated", "allow_hosts": ["127.0.0.1"],
                         "allow_concurrency_writes": True},
            "cases": [{"id": "TC-CON-001", "模块": "订单", "用例标题": "重复提交收敛",
                       "steps": [{"名称": "并发下单", "method": "POST", "path": "/orders",
                                  "headers": {"X-Autotest-Run-ID": "user-cannot-override"},
                                  "并发": {"请求数": 3, "并发度": 3, "总超时": 1000,
                                           "幂等": {"header": "Idempotency-Key", "模式": "共享"}},
                                  "断言": [{"类型": "并发结果", "请求数": 3, "成功数": 3,
                                             "状态码": {"全部在": [201]},
                                             "JSON字段": {"路径": "data.id", "去重数": 1, "非空": True}}]}]}],
        }
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "spec.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(spec, f)
                result = R.run_spec(path, out_dir=os.path.join(temp, "out"), quiet=True)
            self.assertEqual("passed", result["cases"][0]["status"], result["cases"][0]["reason"])
            self.assertEqual(3, len(seen))
            self.assertEqual(1, len({row["idempotency-key"] for row in seen}))
            self.assertEqual(3, len({row["x-autotest-worker"] for row in seen}))
            self.assertEqual(1, len({row["x-autotest-run-id"] for row in seen}))
            self.assertNotIn("user-cannot-override", {row["x-autotest-run-id"] for row in seen})
        finally:
            server.shutdown(); server.server_close(); thread.join()


class TestVirtualService(unittest.TestCase):
    def test_本地虚拟服务返回预设响应(self):
        s = V.VirtualService("pay", [{"match":{"method":"GET","path":"/ok"},"response":{"status":200,"json":{"ok":True}}}])
        url = s.start()
        try:
            from urllib.request import urlopen
            self.assertIn(b'"ok": true', urlopen(url + "/ok").read())
            self.assertEqual(1, len(s.hits))
        finally:
            s.stop()
    def test_故障规则只对当前步骤命中且保留证据(self):
        s = V.VirtualService("pay", [{"match": {"method": "GET", "path": "/ok"},
                                      "response": {"status": 200, "json": {"ok": True}}}])
        url = s.start()
        try:
            fault_id = s.install_fault({"match": {"method": "GET", "path": "/ok"},
                                        "action": "response",
                                        "response": {"status": 503, "json": {"down": True}}})
            with self.assertRaises(URLError):
                urlopen(url + "/ok")
            hit = s.evidence()[0]
            self.assertEqual(fault_id, hit["fault_id"])
            self.assertEqual("response", hit["fault_action"])
            s.remove_fault(fault_id)
            self.assertIn(b'"ok": true', urlopen(url + "/ok").read())
        finally:
            s.stop()

    def test_stop后端口不再接受请求(self):
        s = V.VirtualService("pay", [{"match": {"method": "GET", "path": "/ok"},
                                      "response": {"status": 200, "json": {"ok": True}}}])
        url = s.start()
        s.stop()
        with self.assertRaises(URLError):
            urlopen(url + "/ok", timeout=0.2)


class TestVirtualRunnerLifecycle(unittest.TestCase):
    def _spec(self, isolated=True):
        return {
            "name": "虚拟依赖", "base_url": "http://127.0.0.1:1", "auth": {"type": "none"},
            "autotest": {"mode": "isolated", "allow_hosts": ["127.0.0.1"],
                         "allow_virtualization": isolated},
            "virtual_services": [{"name": "payment", "routes": [
                {"match": {"method": "GET", "path": "/charge"},
                 "response": {"status": 200, "json": {"ok": True}}}
            ]}],
            "cases": [{"id": "TC-VIRTUAL-001", "模块": "依赖", "用例标题": "故障响应",
                       "steps": [{"名称": "替身故障", "method": "GET",
                                  "path": "${virtual.payment.base_url}/charge",
                                  "故障": {"服务": "payment", "match": {"method": "GET", "path": "/charge"},
                                           "action": "response", "response": {"status": 503, "json": {"down": True}}},
                                  "断言": [{"类型": "状态码", "等于": 503}]}]}],
        }

    def test_run_spec启动注入停止并保留故障证据(self):
        with tempfile.TemporaryDirectory() as temp:
            spec_path = os.path.join(temp, "spec.json")
            with open(spec_path, "w", encoding="utf-8") as f:
                json.dump(self._spec(), f)
            result = R.run_spec(spec_path, out_dir=os.path.join(temp, "out"), quiet=True)
            step = result["cases"][0]["steps"][0]
            self.assertEqual("passed", result["cases"][0]["status"])
            base_url = step["virtual"]["base_url"]
            self.assertTrue(step["virtual"]["triggered"])
            self.assertEqual("current_step", step["virtual"]["scope"])
            self.assertEqual(1, len(step["virtual"]["matched_hits"]))
            with self.assertRaises(URLError):
                urlopen(base_url + "/charge", timeout=0.2)

    def test安全拒绝稳定归类blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            spec_path = os.path.join(temp, "spec.json")
            with open(spec_path, "w", encoding="utf-8") as f:
                json.dump(self._spec(isolated=False), f)
            result = R.run_spec(spec_path, out_dir=os.path.join(temp, "out"), quiet=True)
            self.assertEqual("blocked", result["cases"][0]["status"])
            self.assertIn("allow_virtualization", result["cases"][0]["reason"])


if __name__ == "__main__": unittest.main()
