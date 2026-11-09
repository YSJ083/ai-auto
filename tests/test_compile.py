"""compile.py 的编译期校验测试 —— 重点在「写错的东西必须在编译期就被拦下」。

为什么值得单独测：编译层是唯一能在跑之前发现规格错误的地方。漏拦一条，
代价是执行时卡满超时、或者更糟 —— 一条永远通过的用例混进报告里。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import compile as C          # noqa: E402

POLL = {"直到": {"类型": "JSON字段", "路径": "data.online", "等于": True},
        "超时": 30000, "间隔": 1000}


def case(steps, **extra):
    c = {"用例标题": "等设备上线后查详情", "所属模块": "设备", "优先级": "P1",
         "预期结果": "设备状态变为在线，且详情接口返回 200",
         "执行": {"步骤": steps}}
    c.update(extra)
    return c


def compile_one(steps, **extra):
    spec, problems = C.compile_cases({"cases": [case(steps, **extra)]},
                                     base_url="http://h", api_prefix="/api")
    return spec, problems


def errors(problems):
    return [m for lv, m in problems if lv == "error"]


def warnings(problems):
    return [m for lv, m in problems if lv == "warning"]


class TestPollCompile(unittest.TestCase):

    def test_合法轮询可编译(self):
        spec, problems = compile_one([
            {"名称": "等上线", "method": "GET", "path": "/device/1/status",
             "轮询": dict(POLL, path="/device/1/status"),
             "断言": [{"类型": "状态码", "等于": 200}]},
        ])
        self.assertEqual(errors(problems), [])
        self.assertEqual(len(spec["cases"]), 1)
        self.assertEqual(spec["cases"][0]["steps"][0]["轮询"]["间隔"], 1000)

    def test_只有轮询直到没有断言不算缺断言(self):
        # 「直到」本身就是观测点，不该再强制重复写一条断言
        _, problems = compile_one([
            {"名称": "等上线", "method": "GET", "path": "/device/1/status",
             "轮询": POLL},
        ])
        self.assertEqual(errors(problems), [])

    def test_UI步骤配轮询被拦下(self):
        _, problems = compile_one([
            {"ui": "点击", "选择器": "button", "轮询": POLL,
             "断言": [{"类型": "页面", "选择器": "button", "存在": True}]},
        ])
        self.assertTrue(any("只支持 HTTP 步骤" in e for e in errors(problems)))

    def test_轮询缺path被拦下(self):
        _, problems = compile_one([
            {"名称": "等上线", "method": "GET", "轮询": POLL},
        ])
        self.assertTrue(any("没有 path" in e for e in errors(problems)))

    def test_轮询缺直到被拦下(self):
        _, problems = compile_one([
            {"名称": "等上线", "method": "GET", "path": "/x",
             "轮询": {"超时": 1000}, "断言": [{"类型": "状态码", "等于": 200}]},
        ])
        self.assertTrue(any("直到" in e for e in errors(problems)))

    def test_直到引用未定义变量被拦下(self):
        _, problems = compile_one([
            {"名称": "等上线", "method": "GET", "path": "/x",
             "轮询": {"直到": {"类型": "JSON字段", "路径": "data.id",
                              "等于": "${nope}"}},
             "断言": [{"类型": "状态码", "等于": 200}]},
        ])
        self.assertTrue(any("${nope}" in e for e in errors(problems)))

    def test_直到引用前面步骤提取的变量放行(self):
        _, problems = compile_one([
            {"名称": "取ID", "method": "GET", "path": "/x",
             "提取": {"want": "data.id"},
             "断言": [{"类型": "状态码", "等于": 200}]},
            {"名称": "等匹配", "method": "GET", "path": "/y",
             "轮询": {"直到": {"类型": "JSON字段", "路径": "data.id",
                              "等于": "${want}"}}},
        ])
        self.assertEqual(errors(problems), [])

    def test_超时不是数字被拦下(self):
        _, problems = compile_one([
            {"名称": "等上线", "method": "GET", "path": "/x",
             "轮询": {"直到": POLL["直到"], "超时": "30秒"},
             "断言": [{"类型": "状态码", "等于": 200}]},
        ])
        self.assertTrue(any("超时" in e and "数字" in e for e in errors(problems)))

    def test_轮询块未知字段给警告(self):
        _, problems = compile_one([
            {"名称": "等上线", "method": "GET", "path": "/x",
             "轮询": dict(POLL, 重试次数=3),
             "断言": [{"类型": "状态码", "等于": 200}]},
        ])
        self.assertTrue(any("重试次数" in w for w in warnings(problems)))
        self.assertEqual(errors(problems), [])


class TestPollLint(unittest.TestCase):

    def test_lint也能拦下缺直到的轮询(self):
        spec = {"base_url": "http://h", "cases": [{
            "id": "TC-001", "模块": "M", "用例标题": "t", "自动化": "auto",
            "steps": [{"名称": "等", "method": "GET", "path": "/x",
                       "轮询": {"超时": 100},
                       "断言": [{"类型": "状态码", "等于": 200}]}],
        }]}
        problems = C.lint_spec(spec)
        self.assertTrue(any(lv == "error" and "直到" in m for lv, m in problems))

    def test_lint认可只写直到的轮询步骤(self):
        spec = {"base_url": "http://h", "cases": [{
            "id": "TC-001", "模块": "M", "用例标题": "t", "自动化": "auto",
            "steps": [{"名称": "等", "method": "GET", "path": "/x", "轮询": POLL}],
        }]}
        problems = C.lint_spec(spec)
        self.assertEqual([m for lv, m in problems if lv == "error"], [])


class TestNetworkAssertCompile(unittest.TestCase):

    def test_网络请求断言挂UI步骤可编译(self):
        _, problems = compile_one([
            {"ui": "点击", "选择器": "#submit", "path": "/order",
             "断言": [{"类型": "网络", "请求": "/api/order/submit",
                       "次数": 1}]},
        ])
        self.assertEqual(errors(problems), [])

    def test_网络请求断言缺判定条件被拦下(self):
        _, problems = compile_one([
            {"ui": "点击", "选择器": "#submit",
             "断言": [{"类型": "网络", "请求": "/api/order/submit"}]},
        ])
        self.assertTrue(any("判定条件" in e for e in errors(problems)))


class TestArtifactCompile(unittest.TestCase):
    """上传/下载写错的代价是「判不了」或「读到错的文件」，编译期就该拦。"""

    def test_合法上传可编译(self):
        _, problems = compile_one([
            {"ui": "上传", "选择器": "input[type=file]", "文件": "/tmp/a.pdf",
             "断言": [{"类型": "页面", "选择器": ".ok", "可见": True}]},
        ])
        self.assertEqual(errors(problems), [])

    def test_文件与下载目录不再算未知字段(self):
        _, problems = compile_one([
            {"ui": "上传", "选择器": "#f", "文件": ["/tmp/a.pdf"],
             "断言": [{"类型": "页面", "选择器": ".ok", "可见": True}]},
        ])
        self.assertFalse(any("未知字段" in w for w in warnings(problems)))

    def test_上传缺文件被拦下(self):
        _, problems = compile_one([
            {"ui": "上传", "选择器": "#f",
             "断言": [{"类型": "页面", "选择器": ".ok", "可见": True}]},
        ])
        self.assertTrue(any("没有「文件」" in e for e in errors(problems)))

    def test_上传缺选择器被拦下(self):
        _, problems = compile_one([
            {"ui": "上传", "文件": "/tmp/a.pdf",
             "断言": [{"类型": "页面", "选择器": ".ok", "可见": True}]},
        ])
        self.assertTrue(any("input[type=file]" in e for e in errors(problems)))

    def test_文件写错类型被拦下(self):
        _, problems = compile_one([
            {"ui": "上传", "选择器": "#f", "文件": 123,
             "断言": [{"类型": "页面", "选择器": ".ok", "可见": True}]},
        ])
        self.assertTrue(any("路径" in e for e in errors(problems)))

    def test_非上传步骤写文件只是警告(self):
        _, problems = compile_one([
            {"ui": "点击", "选择器": "#f", "文件": "/tmp/a.pdf",
             "断言": [{"类型": "页面", "选择器": ".ok", "可见": True}]},
        ])
        self.assertEqual(errors(problems), [])
        self.assertTrue(any("会被忽略" in w for w in warnings(problems)))

    def test_下载断言没有下载动作被拦下(self):
        # 这是最容易写出的错：断言写了，下载动作漏了，执行时才说「判不了」
        _, problems = compile_one([
            {"method": "GET", "path": "/export",
             "断言": [{"类型": "下载", "后缀": ".xlsx"}]},
        ])
        self.assertTrue(any("没有下载动作" in e for e in errors(problems)))

    def test_HTTP下载目录配下载断言可编译(self):
        _, problems = compile_one([
            {"method": "GET", "path": "/export", "下载目录": "/tmp/dl",
             "断言": [{"类型": "下载", "后缀": ".xlsx"}]},
        ])
        self.assertEqual(errors(problems), [])

    def test_UI下载动作配下载断言可编译(self):
        _, problems = compile_one([
            {"ui": "下载", "选择器": "#export", "下载目录": "/tmp/dl",
             "断言": [{"类型": "下载", "名称包含": "导出"}]},
        ])
        self.assertEqual(errors(problems), [])

    def test_UI下载同一步既导航又下载给警告(self):
        _, problems = compile_one([
            {"ui": "下载", "选择器": "#export", "path": "/report",
             "断言": [{"类型": "下载", "后缀": ".pdf"}]},
        ])
        self.assertTrue(any("拆成两步" in w for w in warnings(problems)))

    def test_下载目录配在别的动作上给警告(self):
        _, problems = compile_one([
            {"ui": "点击", "选择器": "#x", "下载目录": "/tmp/dl",
             "断言": [{"类型": "页面", "选择器": ".ok", "可见": True}]},
        ])
        self.assertTrue(any("会被忽略" in w for w in warnings(problems)))

    def test_多条下载断言只判最后一个给警告(self):
        _, problems = compile_one([
            {"method": "GET", "path": "/export", "下载目录": "/tmp/dl",
             "断言": [{"类型": "下载", "后缀": ".xlsx"},
                      {"类型": "下载", "最小字节": 10}]},
        ])
        self.assertTrue(any("最后一个" in w for w in warnings(problems)))


class VagueWordTest(unittest.TestCase):
    """含糊措辞的匹配：英文按词边界，中文按子串。

    `token` 里含 `ok`，早期用子串判断会把「返回 data.token 非空」这种
    完全可判定的预期结果拦下来。假阳性比漏检更伤：写用例的人只会把
    token 改个说法绕过去，检查形同虚设。
    """

    def test_英文词不匹配单词内部(self):
        from core.asserts import find_vague
        self.assertEqual([], find_vague("返回 data.token 非空"))

    def test_英文词大写也认(self):
        from core.asserts import find_vague
        self.assertEqual(["ok"], find_vague("返回 OK 就行"))

    def test_中文含糊词照旧拦下(self):
        from core.asserts import find_vague
        self.assertEqual(["正常"], find_vague("页面显示正常"))

    def test_可判定的预期结果不误报(self):
        _, problems = compile_one([
            {"method": "GET", "path": "/login",
             "断言": [{"类型": "JSON字段", "路径": "data.token", "非空": True}]},
        ], 预期结果="返回 code=0 且 data.token 非空")
        self.assertEqual([], [m for lv, m in problems if "不可判定" in m])


class TestVirtualServicesCompile(unittest.TestCase):
    def _spec(self, fault=None, virtual_services=None, path="/health"):
        return {"base_url": "http://sut", "auth": {"type": "none"},
                "virtual_services": virtual_services if virtual_services is not None else [{
                    "name": "payment", "routes": [{
                        "match": {"method": "GET", "path": "/health"},
                        "response": {"status": 200, "json": {"ok": True}},
                    }]}],
                "cases": [{"id": "TC-VIRTUAL-001", "模块": "M", "用例标题": "替身故障",
                           "steps": [{"method": "GET", "path": path, "故障": fault,
                                      "断言": [{"类型": "状态码", "等于": 503}]}]}]}

    def test_合法虚拟服务与故障规则通过lint(self):
        fault = {"服务": "payment", "match": {"method": "GET", "path": "/health"},
                 "action": "response", "response": {"status": 503, "json": {"down": True}}}
        errors = [m for lv, m in C.lint_spec(self._spec(fault, path="${virtual.payment.base_url}/health")) if lv == "error"]
        self.assertEqual([], errors)

    def test_故障规则必须匹配已声明route(self):
        fault = {"服务": "payment", "match": {"method": "GET", "path": "/missing"},
                 "action": "reset"}
        errors = [m for lv, m in C.lint_spec(self._spec(fault)) if lv == "error"]
        self.assertTrue(any("匹配不到" in m for m in errors))

    def test_故障步骤必须有断言(self):
        fault = {"服务": "payment", "match": {"method": "GET", "path": "/health"}, "action": "reset"}
        spec = self._spec(fault)
        spec["cases"][0]["steps"][0].pop("断言")
        errors = [m for lv, m in C.lint_spec(spec) if lv == "error"]
        self.assertTrue(any("故障步骤必须有断言" in m for m in errors))

    def test_未声明的虚拟变量被拦下(self):
        fault = {"服务": "payment", "match": {"method": "GET", "path": "/health"}, "action": "reset"}
        errors = [m for lv, m in C.lint_spec(self._spec(fault, path="${virtual.nope.base_url}/health")) if lv == "error"]
        self.assertTrue(any("未声明" in m for m in errors))

