"""JUnit XML 输出的单元测试 —— 不联网。

为什么值得钉：
    junit.xml 是给 CI 读的，人不会打开它看。写坏了不会有人当场发现，
    表现为「流水线里全是绿的」或者「一条结果都解析不出来」。
    两条都属于「报告在骗人」，只是骗的是机器。

最关键的一条是状态映射：阻断必须落成 <error>，不能落成 <skipped>。
skipped 在多数 CI 里不算失败，等于把「这轮根本没测成」伪装成「不需要测」。
"""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import junit as J      # noqa: E402


def case(cid, status, module="登录", **kw):
    c = {"id": cid, "title": "标题", "module": module, "status": status,
         "duration_ms": 120, "reason": "", "steps": [],
         "assertions": {"passed": 1, "total": 1}}
    c.update(kw)
    return c


def result(cases):
    return {
        "tool": {"name": "ai-auto-test", "version": "1.7.0"},
        "run": {"spec_name": "登录回归", "started_at": "2026-10-22 15:00:00",
                "duration_ms": 2500},
        "target": {"env": "test", "base": "http://10.0.0.1"},
        "cases": cases,
    }


def parse(xml_text):
    return ET.fromstring(xml_text.encode("utf-8"))


class MappingTest(unittest.TestCase):
    """五种状态 → JUnit 元素的映射。"""

    def test_通过不产生子元素(self):
        root = parse(J.render_junit(result([case("TC-1", "passed")])))
        tc = root.find("./testsuite/testcase")
        self.assertEqual([], list(tc))

    def test_失败落成failure并带期望与实际(self):
        steps = [{"name": "登录", "checks": [
            {"type": "状态码", "expected": "200", "actual": "401", "passed": False}]}]
        root = parse(J.render_junit(result([case("TC-1", "failed", steps=steps)])))
        f = root.find("./testsuite/testcase/failure")
        self.assertIsNotNone(f)
        self.assertEqual("assertion", f.get("type"))
        self.assertIn("401", f.text or "")
        self.assertIn("期望：200", f.text or "")

    def test_阻断落成error而不是skipped(self):
        # 最重要的一条：阻断被记成 skipped 的话，CI 会显示「跳过」，流水线还是绿的
        root = parse(J.render_junit(result([case("TC-1", "blocked", reason="连不上")])))
        tc = root.find("./testsuite/testcase")
        self.assertIsNotNone(tc.find("error"))
        self.assertIsNone(tc.find("skipped"))
        self.assertEqual("environment", tc.find("error").get("type"))

    def test_跳过落成skipped(self):
        root = parse(J.render_junit(result([case("TC-1", "skipped", reason="依赖未通过")])))
        self.assertIsNotNone(root.find("./testsuite/testcase/skipped"))

    def test_手动落成skipped并说明原因(self):
        root = parse(J.render_junit(result([case("TC-1", "manual")])))
        sk = root.find("./testsuite/testcase/skipped")
        self.assertIsNotNone(sk)
        self.assertIn("人工", sk.get("message"))

    def test_汇总计数与状态一致(self):
        root = parse(J.render_junit(result([
            case("TC-1", "passed"), case("TC-2", "failed"),
            case("TC-3", "blocked"), case("TC-4", "skipped"),
            case("TC-5", "manual")])))
        self.assertEqual("5", root.get("tests"))
        self.assertEqual("1", root.get("failures"))
        self.assertEqual("1", root.get("errors"))
        self.assertEqual("2", root.get("skipped"))   # skipped + manual


class ShapeTest(unittest.TestCase):

    def test_按模块分组(self):
        root = parse(J.render_junit(result([
            case("TC-1", "passed", module="登录"),
            case("TC-2", "passed", module="鉴权"),
            case("TC-3", "passed", module="登录")])))
        suites = {s.get("name"): s for s in root.findall("testsuite")}
        self.assertEqual({"登录", "鉴权"}, set(suites))
        self.assertEqual("2", suites["登录"].get("tests"))

    def test_时间是三位小数秒(self):
        root = parse(J.render_junit(result([case("TC-1", "passed", duration_ms=1500)])))
        self.assertEqual("1.500", root.find("./testsuite/testcase").get("time"))

    def test_用例名带id便于定位(self):
        root = parse(J.render_junit(result([case("TC-LOGIN-001", "passed")])))
        self.assertIn("TC-LOGIN-001", root.find("./testsuite/testcase").get("name"))

    def test_环境信息进properties(self):
        root = parse(J.render_junit(result([case("TC-1", "passed")])))
        props = {p.get("name"): p.get("value")
                 for p in root.findall("./testsuite/properties/property")}
        self.assertEqual("test", props["env"])
        self.assertEqual("ai-auto-test 1.7.0", props["tool"])


class EscapeTest(unittest.TestCase):
    """转义写错会让 CI 解析直接失败，属于「整份结果消失」级别的问题。"""

    def test_模块名里的特殊字符能解析(self):
        name = 'A&B<C>"D"'
        root = parse(J.render_junit(result([case("TC-1", "passed", module=name)])))
        self.assertEqual(name, root.find("./testsuite").get("name"))

    def test_失败原因里的特殊字符能解析(self):
        reason = '期望 <ok> & 实际 "fail"'
        root = parse(J.render_junit(result([case("TC-1", "failed", reason=reason)])))
        self.assertEqual(reason, root.find("./testsuite/testcase/failure").get("message"))

    def test_控制字符被清掉而不是让文件坏掉(self):
        # 响应体里偶尔会夹 \x00，XML 1.0 不允许，留着整份文件就废了
        root = parse(J.render_junit(result([
            case("TC-1", "failed", reason="坏\x00字符\x01")])))
        msg = root.find("./testsuite/testcase/failure").get("message")
        self.assertNotIn("\x00", msg)
        self.assertIn("坏", msg)

    def test_空结果也能生成合法xml(self):
        root = parse(J.render_junit(result([])))
        self.assertEqual("0", root.get("tests"))


class WriteTest(unittest.TestCase):

    def test_落盘并带xml声明(self):
        with tempfile.TemporaryDirectory() as d:
            path = J.write_junit(result([case("TC-1", "passed")]),
                                 os.path.join(d, "sub", "junit.xml"))
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(text.startswith("<?xml"))
            parse(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
