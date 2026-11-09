import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import contract as C
from core import compile as Compile
from core import runner as R


def response(body, status=200, content_type="application/json"):
    return {"status": status, "elapsed_ms": 1, "json": body, "text": "{}",
            "headers": {"Content-Type": content_type}, "request_headers": {}, "error": ""}


class TestBuiltinContract(unittest.TestCase):
    def setUp(self):
        self.registry = C.build_registry({"schemas": {
            "User": {"type": "object", "required": ["id", "name"], "properties": {
                "id": {"type": "integer"}, "name": {"type": "string", "minLength": 1}},
                "additionalProperties": False}}})
        self.assertion = {"类型": "响应契约", "schema": "User", "状态码": 200,
                          "内容类型": "application/json"}

    def test_合法响应通过(self):
        ok, detail, evidence = C.validate_response(self.assertion, response({"id": 1, "name": "A"}), self.registry)
        self.assertTrue(ok, detail)
        self.assertEqual([], evidence["violations"])

    def test_缺字段与类型错均失败(self):
        ok, detail, evidence = C.validate_response(self.assertion, response({"id": "1"}), self.registry)
        self.assertFalse(ok)
        self.assertIn("缺少必填字段 name", detail)
        self.assertGreaterEqual(len(evidence["violations"]), 2)

    def test_内容类型带charset仍通过(self):
        ok, _, _ = C.validate_response(self.assertion, response({"id": 1, "name": "A"}, content_type="application/json; charset=utf-8"), self.registry)
        self.assertTrue(ok)

    def test_非json响应失败(self):
        r = response(None, content_type="text/html")
        r["text"] = "<html>error</html>"
        ok, detail, _ = C.validate_response(self.assertion, r, self.registry)
        self.assertFalse(ok)
        self.assertIn("Content-Type", detail)


class TestContractCompile(unittest.TestCase):
    def test_不存在schema在lint期拦截(self):
        spec = {"base_url": "http://x", "contract": {"schemas": {}}, "cases": [{
            "id": "TC-C-001", "steps": [{"path": "/x", "断言": [{"类型": "响应契约", "schema": "Missing"}]}]}]}
        messages = [m for level, m in Compile.lint_spec(spec) if level == "error"]
        self.assertTrue(any("schema 不存在" in m for m in messages))

    def test_UI步骤禁止响应契约(self):
        spec = {"base_url": "http://x", "contract": {"schemas": {"S": {"type": "object"}}}, "cases": [{
            "id": "TC-C-002", "steps": [{"ui": "点击", "选择器": "#go", "断言": [{"类型": "响应契约", "schema": "S"}]}]}]}
        messages = [m for level, m in Compile.lint_spec(spec) if level == "error"]
        self.assertTrue(any("只能挂在 HTTP 步骤" in m for m in messages))


class TestContractRunner(unittest.TestCase):
    def test_契约失败属于failed并保留结构化证据(self):
        registry = C.build_registry({"schemas": {"S": {"type": "object", "required": ["id"]}}})
        class Client:
            cookies = {}
            def request(self, method, url, **kw):
                return dict(response({}), method=method, url=url)
        steps, failures, net_only = R._run_steps(Client(), "http://x", [{"path": "/x", "断言": [{"类型": "响应契约", "schema": "S"}]}], {"__contract_registry": registry}, {})
        self.assertTrue(failures)
        self.assertFalse(net_only)
        self.assertIn("contract", steps[0]["checks"][0])


if __name__ == "__main__":
    unittest.main()
