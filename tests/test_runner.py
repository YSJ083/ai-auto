"""runner.py 轮询逻辑的单元测试 —— 不联网、不起浏览器。

为什么单独钉这部分：轮询的失败方式很隐蔽。写错时它不会崩，会先卡满超时
（默认 30 秒），再报一条看起来像产品缺陷的失败 —— 而真正的问题是规格。
更糟的一种是「条件恒真」，那一步会瞬间通过，什么也没等。

这里用假 client 把这几类都试出来：成立即过、多次后成立、超时、全程异常、
条件写错、请求字段覆盖。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import runner as R          # noqa: E402


def resp(status=200, json_body=None, text="", error=""):
    return {"status": status, "elapsed_ms": 1, "json": json_body, "text": text,
            "headers": {}, "request_headers": {}, "error": error}


class FakeClient(object):
    """按顺序吐出预设响应；列表用尽后重复最后一个。元素是异常就抛出。

    同时记录每次请求的 URL，用来验证「轮询块里的 path 会覆盖步骤的 path」。
    """

    ui_problem = ""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = {}

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self.responses[0]
        if len(self.responses) > 1:
            item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return dict(item, url=url, method=method)


def step(poll=None, **extra):
    s = {"名称": "等状态", "method": "GET", "path": "/device/1/status"}
    if poll is not None:
        s["轮询"] = poll
    s.update(extra)
    return s


def poll(until=None, timeout=300, interval=1, **extra):
    p = {"直到": until if until is not None else
         {"类型": "JSON字段", "路径": "data.online", "等于": True},
         "超时": timeout, "间隔": interval}
    p.update(extra)
    return p


class TestPollSuccess(unittest.TestCase):

    def test_条件一次就成立只请求一次(self):
        c = FakeClient([resp(json_body={"data": {"online": True}})])
        r, info, fail = R._poll_http(c, "http://x/api", step(poll()), {}, {})
        self.assertIsNone(fail)
        self.assertEqual(info["次数"], 1)
        self.assertEqual(len(c.calls), 1)
        self.assertTrue(r["json"]["data"]["online"])

    def test_多次之后成立返回最后一次响应(self):
        c = FakeClient([resp(json_body={"data": {"online": False, "n": 1}}),
                        resp(json_body={"data": {"online": False, "n": 2}}),
                        resp(json_body={"data": {"online": True, "n": 3}})])
        r, info, fail = R._poll_http(c, "http://x/api", step(poll()), {}, {})
        self.assertIsNone(fail)
        self.assertEqual(info["次数"], 3)
        self.assertEqual(r["json"]["data"]["n"], 3)

    def test_直到可以写多条且必须全部成立(self):
        c = FakeClient([resp(json_body={"data": {"online": True, "ready": False}}),
                        resp(json_body={"data": {"online": True, "ready": True}})])
        until = [{"类型": "JSON字段", "路径": "data.online", "等于": True},
                 {"类型": "JSON字段", "路径": "data.ready", "等于": True}]
        _, info, fail = R._poll_http(c, "http://x/api", step({"直到": until,
                                                             "超时": 300, "间隔": 1}), {}, {})
        self.assertIsNone(fail)
        self.assertEqual(info["次数"], 2)

    def test_直到里的变量会被插值(self):
        c = FakeClient([resp(json_body={"data": {"id": "A1"}})])
        until = {"类型": "JSON字段", "路径": "data.id", "等于": "${want}"}
        _, _, fail = R._poll_http(c, "http://x/api",
                                  step({"直到": until, "超时": 100, "间隔": 1}),
                                  {"want": "A1"}, {})
        self.assertIsNone(fail)


class TestPollTimeout(unittest.TestCase):

    def test_超时后报失败并带上最后一次实际值(self):
        c = FakeClient([resp(json_body={"data": {"online": False}})])
        r, info, fail = R._poll_http(c, "http://x/api",
                                     step(poll(timeout=120, interval=20)), {}, {})
        self.assertTrue(info["超时"])
        self.assertIn("轮询超时", fail)
        self.assertIn("data.online=false", fail)    # 最后一次实际值要能被直接看懂
        self.assertGreaterEqual(info["次数"], 2)
        self.assertEqual(r["status"], 200)      # 拿到了真实响应，不算通道失败

    def test_超时为0时至少请求一次(self):
        c = FakeClient([resp(json_body={"data": {"online": False}})])
        _, info, fail = R._poll_http(c, "http://x/api",
                                     step(poll(timeout=0, interval=1)), {}, {})
        self.assertEqual(info["次数"], 1)
        self.assertIn("轮询超时", fail)

    def test_间隔写0不会死循环(self):
        c = FakeClient([resp(json_body={"data": {"online": False}})])
        _, info, _ = R._poll_http(c, "http://x/api",
                                  step(poll(timeout=100, interval=0)), {}, {})
        # 间隔被抬到下限 50ms，100ms 内最多几次；关键是有界、能退出
        self.assertLessEqual(info["次数"], 5)


class TestPollNetworkError(unittest.TestCase):

    def test_中途异常不立即判失败(self):
        c = FakeClient([ConnectionError("boom"),
                        resp(status=502, json_body=None),
                        resp(json_body={"data": {"online": True}})])
        r, info, fail = R._poll_http(c, "http://x/api",
                                     step(poll(timeout=300, interval=1)), {}, {})
        self.assertIsNone(fail)
        self.assertEqual(info["次数"], 3)

    def test_全程异常且超时按通道失败处理(self):
        c = FakeClient([ConnectionError("refused")])
        r, info, fail = R._poll_http(c, "http://x/api",
                                     step(poll(timeout=100, interval=10)), {}, {})
        self.assertEqual(r["status"], 0)         # 上层据此判 blocked 而不是 failed
        self.assertIn("没拿到响应", fail)
        self.assertIn("refused", fail)


class TestPollSpecError(unittest.TestCase):

    def test_直到写错时报出来不抛异常(self):
        c = FakeClient([resp(json_body={"data": {}})])
        bad = {"类型": "JSON字段", "路径": "data.online"}     # 缺判定条件
        r, info, fail = R._poll_http(c, "http://x/api",
                                     step({"直到": bad, "超时": 50, "间隔": 1}), {}, {})
        self.assertTrue(info["超时"])
        self.assertIn("写错了", fail)
        self.assertEqual(len(c.calls), 1)        # 立刻停，不空转到超时


class TestPollRequestShape(unittest.TestCase):

    def test_轮询块里的path覆盖步骤path(self):
        c = FakeClient([resp(json_body={"data": {"online": True}})])
        R._poll_http(c, "http://x/api",
                     step(poll(path="/poll/here"), path="/ignored"), {}, {})
        self.assertTrue(c.calls[0]["url"].endswith("/poll/here"))

    def test_轮询块里的method与body会生效(self):
        c = FakeClient([resp(json_body={"data": {"online": True}})])
        R._poll_http(c, "http://x/api",
                     step(poll(method="POST", json={"a": 1})), {}, {})
        self.assertEqual(c.calls[0]["method"], "POST")
        self.assertEqual(c.calls[0]["json_body"], {"a": 1})

    def test_断言和提取不会被带进请求(self):
        c = FakeClient([resp(json_body={"data": {"online": True}})])
        s = step(poll(), 断言=[{"类型": "状态码", "等于": 200}], 提取={"x": "data.online"})
        req = R._poll_request_step(s, s["轮询"])
        self.assertNotIn("断言", req)
        self.assertNotIn("提取", req)
        self.assertNotIn("轮询", req)


class TestPollWiring(unittest.TestCase):
    """轮询接进 _run_steps 之后的行为：结果落盘、UI 步骤被拦下。"""

    def test_轮询结果写进步骤并参与判定(self):
        c = FakeClient([resp(json_body={"data": {"online": False}}),
                        resp(json_body={"data": {"online": True, "code": "OK"}})])
        s = step(poll(timeout=300, interval=1),
                 断言=[{"类型": "JSON字段", "路径": "data.code", "等于": "OK"}])
        steps, fails, _ = R._run_steps(c, "http://x/api", [s], {}, {})
        self.assertEqual(fails, [])
        self.assertEqual(steps[0]["poll"]["次数"], 2)
        self.assertTrue(steps[0]["passed"])

    def test_轮询超时的根因排在下游断言之前(self):
        c = FakeClient([resp(json_body={"data": {"online": False, "code": "NO"}})])
        s = step(poll(timeout=60, interval=10),
                 断言=[{"类型": "JSON字段", "路径": "data.code", "等于": "OK"}])
        steps, fails, _ = R._run_steps(c, "http://x/api", [s], {}, {})
        self.assertTrue(fails)
        self.assertEqual(steps[0]["checks"][0]["assertion"]["类型"], "轮询")
        self.assertIn("轮询超时", steps[0]["checks"][0]["actual"])

    def test_轮询写在UI步骤上被明确拦下(self):
        c = FakeClient([resp()])
        s = {"ui": "点击", "选择器": "button", "轮询": poll()}
        steps, fails, _ = R._run_steps(c, "http://x/api", [s], {}, {})
        self.assertIn("只支持 HTTP 步骤", fails[0])
        self.assertEqual(steps[0]["poll"], {"次数": 0, "未执行": True})

    def test_没有轮询的普通步骤poll字段为空(self):
        c = FakeClient([resp(json_body={"ok": 1})])
        s = {"名称": "普通", "method": "GET", "path": "/x",
             "断言": [{"类型": "状态码", "等于": 200}]}
        steps, fails, _ = R._run_steps(c, "http://x/api", [s], {}, {})
        self.assertEqual(fails, [])
        self.assertIsNone(steps[0]["poll"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
