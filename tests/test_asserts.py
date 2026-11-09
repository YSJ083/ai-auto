"""asserts.py 判定逻辑的单元测试 —— 不联网、不依赖被测系统、毫秒级。

为什么要有这份测试：
13 类断言里有一半从未在真实系统上执行过（耗时 / 业务状态 / 包含 / 响应头 /
路径存在 / 控制台 / 网络），它们的判定代码此前只被人眼看过。断言判错的后果不是报错，
而是**报告说通过、实际是坏的** —— 这比崩溃危险得多，因为人会信它。

所以这里把每一类的「通过 / 不通过 / 边界 / 写错时的报错」都钉死：
一旦有人改坏判定逻辑，这里会立刻红，而不是等到某天拿一份失真报告去交差。

运行：
    cd ~/.workbuddy/skills/ai-auto-test
    python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import asserts as A          # noqa: E402
from core.asserts import AssertError   # noqa: E402


# ---------------------------------------------------------------- 构造工具

def resp(status=200, json_body=None, text="", headers=None, elapsed=10,
         ui=False, url="", error=None, downloads=None):
    """造一个与 runner 产出同形状的响应。"""
    r = {"status": status, "json": json_body, "text": text,
         "headers": headers or {}, "elapsed_ms": elapsed, "ui": ui, "url": url}
    if error:
        r["error"] = error
    if downloads is not None:
        r["downloads"] = downloads
    return r


def artifact(name="导出.xlsx", size=20480, path="/tmp/out/导出.xlsx",
             sha="abc123def4567890", error="", truncated=False):
    """一个下载产物。path 传空串表示没落盘。"""
    return {"name": name, "size": size, "path": path, "sha256": sha,
            "url": "http://host/export", "error": error, "truncated": truncated}


def page(elements=None, console_errors=None, page_errors=None,
         network_failures=None, network_requests=None, network_truncated=False,
         url="http://host/home"):
    """造一个 UI 步骤的页面快照体（judge 会把它当作 resp["json"]）。"""
    out = {"url": url,
           "elements": elements or {},
           "console_errors": console_errors or [],
           "page_errors": page_errors or [],
           "network_failures": network_failures or []}
    if network_requests is not None:
        out["network_requests"] = network_requests
        out["network_truncated"] = network_truncated
    return out


def req(url, method="GET", status=200, body=""):
    return {"seq": 1, "url": url, "method": method, "status": status,
            "type": "xhr", "body": body}


def el(count=1, exists=True, visible=True, text="", value=None,
       enabled=True, attrs=None):
    return {"count": count, "exists": exists, "visible": visible,
            "text": text, "value": value, "enabled": enabled,
            "attrs": attrs or {}}


def judge(assertion, r, variables=None):
    return A.judge(assertion, r, variables or {})


# ---------------------------------------------------------------- 全覆盖守卫

class TestCoverage(unittest.TestCase):
    """保证 13 类断言每类都有可执行的最小写法，防止某一类变成死代码。"""

    SAMPLES = {
        "状态码": ({"类型": "状态码", "等于": 200}, lambda: resp()),
        "耗时": ({"类型": "耗时", "小于": 100}, lambda: resp(elapsed=10)),
        "业务状态": ({"类型": "业务状态", "等于": 0},
                 lambda: resp(json_body={"status": 0})),
        "成功标志": ({"类型": "成功标志", "等于": True},
                 lambda: resp(json_body={"success": True})),
        "包含": ({"类型": "包含", "值": "ok"}, lambda: resp(text="all ok")),
        "不包含": ({"类型": "不包含", "值": "error"}, lambda: resp(text="ok")),
        "JSON字段": ({"类型": "JSON字段", "路径": "data.id", "非空": True},
                  lambda: resp(json_body={"data": {"id": 1}})),
        "响应头": ({"类型": "响应头", "名称": "Content-Type", "包含": "json"},
                lambda: resp(headers={"Content-Type": "application/json"})),
        "路径存在": ({"类型": "路径存在", "路径": "data"},
                 lambda: resp(json_body={"data": {}})),
        "响应契约": (None, None),  # 需由 runner 注入已解析的契约 registry，见 test_contract.py
        "并发结果": (None, None),
        "DB": (None, None),
        "MQ": (None, None),
        "Trace": (None, None),
        "页面": ({"类型": "页面", "选择器": "h1", "可见": True},
               lambda: resp(ui=True, json_body=page({"h1": el()}))),
        "URL": ({"类型": "URL", "包含": "/home"},
                lambda: resp(ui=True, json_body=page())),
        "控制台": ({"类型": "控制台", "无错误": True},
                lambda: resp(ui=True, json_body=page())),
        "网络": ({"类型": "网络", "无失败请求": True},
               lambda: resp(ui=True, json_body=page())),
        "下载": ({"类型": "下载", "后缀": ".xlsx"},
               lambda: resp(downloads=[artifact()])),
    }

    def test_样本表与代码里的类型集完全一致(self):
        self.assertEqual(set(self.SAMPLES), set(A.SUPPORTED))

    def test_每个类型的最小写法都能判出通过(self):
        for kind, (assertion, make) in self.SAMPLES.items():
            if assertion is None:
                continue
            with self.subTest(kind=kind):
                ok, detail = judge(assertion, make())
                self.assertTrue(ok, "%s 最小写法应当通过，实际：%s" % (kind, detail))

    def test_每个类型都声明了至少一个判定条件(self):
        for kind, conds in A.SUPPORTED.items():
            with self.subTest(kind=kind):
                self.assertTrue(conds, "%s 没有声明任何判定条件" % kind)


# ---------------------------------------------------------------- 形状校验

class TestShape(unittest.TestCase):

    def test_缺类型报规格错误(self):
        with self.assertRaises(AssertError):
            judge({"等于": 200}, resp())

    def test_未知类型报规格错误并列出可用类型(self):
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "数据库"}, resp())
        self.assertIn("未知断言类型", str(cm.exception))
        self.assertIn("状态码", str(cm.exception))

    def test_断言不是对象时报规格错误(self):
        with self.assertRaises(AssertError):
            judge("状态码=200", resp())

    def test_只给类型不给条件报规格错误(self):
        with self.assertRaises(AssertError):
            judge({"类型": "状态码"}, resp())

    def test_缺必需键时报规格错误而不是KeyError(self):
        """缺键属于「规格写错」，必须能被 runner 捕获成规格错误。

        如果抛 KeyError，runner 只 catch AssertError，整个执行会带堆栈崩掉，
        用户看到的是 traceback 而不是「规格错误：…」。
        """
        cases = [
            ({"类型": "耗时"}, "耗时"),
            ({"类型": "响应头"}, "响应头"),
            ({"类型": "包含"}, "包含"),
            ({"类型": "不包含"}, "不包含"),
            ({"类型": "路径存在"}, "路径存在"),
        ]
        for assertion, label in cases:
            with self.subTest(label=label):
                with self.assertRaises(AssertError):
                    judge(assertion, resp(text="ok", json_body={}))

    def test_期望在给了非二元组时报规格错误(self):
        with self.assertRaises(AssertError):
            judge({"类型": "JSON字段", "路径": "n", "期望在": 5},
                  resp(json_body={"n": 5}))


# ---------------------------------------------------------------- HTTP 层

class TestStatusCode(unittest.TestCase):

    def test_等于_命中(self):
        self.assertTrue(judge({"类型": "状态码", "等于": 200}, resp())[0])

    def test_等于_未命中(self):
        ok, detail = judge({"类型": "状态码", "等于": 201}, resp(status=200))
        self.assertFalse(ok)
        self.assertIn("200", detail)

    def test_等于_字符串与数字等价(self):
        self.assertTrue(judge({"类型": "状态码", "等于": "200"}, resp())[0])

    def test_不等于(self):
        self.assertTrue(judge({"类型": "状态码", "不等于": 500}, resp())[0])
        self.assertFalse(judge({"类型": "状态码", "不等于": 200}, resp())[0])

    def test_在(self):
        self.assertTrue(judge({"类型": "状态码", "在": [200, 201]}, resp())[0])
        self.assertFalse(judge({"类型": "状态码", "在": [301, 302]}, resp())[0])

    def test_UI步骤无导航时拒绝判定(self):
        """UI 步骤没导航就没有状态码。

        以前这里返回 200，于是挂在点击步骤上的「状态码 200」恒为真 ——
        一条永远通过的断言比没有断言更危险，因为它会让人以为验过了。
        """
        r = resp(status=None, ui=True)
        with self.assertRaises(AssertError) as ctx:
            judge({"类型": "状态码", "等于": 200}, r)
        self.assertIn("没有 HTTP 状态码", str(ctx.exception))

    def test_UI步骤有导航时正常判定(self):
        r = resp(status=200, ui=True)
        self.assertTrue(judge({"类型": "状态码", "等于": 200}, r)[0])

    def test_HTTP步骤不受影响(self):
        # HTTP 通道永远有真实状态码，这条守卫不能误伤它
        self.assertTrue(judge({"类型": "状态码", "等于": 200}, resp(status=200))[0])


class TestElapsed(unittest.TestCase):

    def test_小于_命中(self):
        self.assertTrue(judge({"类型": "耗时", "小于": 500}, resp(elapsed=100))[0])

    def test_小于_超时(self):
        ok, detail = judge({"类型": "耗时", "小于": 50}, resp(elapsed=100))
        self.assertFalse(ok)
        self.assertIn("100", detail)

    def test_阈值可被变量填充(self):
        self.assertTrue(
            judge({"类型": "耗时", "小于": "${budget}"}, resp(elapsed=100),
                  {"budget": "500"})[0])

    def test_边界_恰好等于阈值算不通过(self):
        """严格小于：100 不满足 <100。"""
        self.assertFalse(judge({"类型": "耗时", "小于": 100}, resp(elapsed=100))[0])


class TestHeader(unittest.TestCase):

    def test_名称匹配忽略大小写(self):
        r = resp(headers={"Content-Type": "application/json"})
        self.assertTrue(
            judge({"类型": "响应头", "名称": "content-type", "包含": "json"}, r)[0])

    def test_等于(self):
        r = resp(headers={"X-Trace": "abc"})
        self.assertTrue(judge({"类型": "响应头", "名称": "x-trace", "等于": "abc"}, r)[0])
        self.assertFalse(judge({"类型": "响应头", "名称": "x-trace", "等于": "abd"}, r)[0])

    def test_正则(self):
        r = resp(headers={"Set-Cookie": "sid=abc123; Path=/"})
        self.assertTrue(
            judge({"类型": "响应头", "名称": "Set-Cookie", "正则": r"sid=\w+"}, r)[0])
        self.assertFalse(
            judge({"类型": "响应头", "名称": "Set-Cookie", "正则": r"sid=\d+"}, r)[0])

    def test_头不存在时不报错只判失败(self):
        ok, detail = judge({"类型": "响应头", "名称": "X-None", "包含": "a"}, resp())
        self.assertFalse(ok)
        self.assertIn("None", detail)

    def test_缺少判定条件报规格错误(self):
        with self.assertRaises(AssertError):
            judge({"类型": "响应头", "名称": "X"}, resp())


# ---------------------------------------------------------------- 业务层

class TestBusinessStatus(unittest.TestCase):

    def test_等于(self):
        self.assertTrue(judge({"类型": "业务状态", "等于": 0},
                              resp(json_body={"status": 0}))[0])
        self.assertFalse(judge({"类型": "业务状态", "等于": 0},
                               resp(json_body={"status": 500}))[0])

    def test_没有json时不崩只判失败(self):
        ok, _ = judge({"类型": "业务状态", "等于": 0}, resp(json_body=None))
        self.assertFalse(ok)

    def test_body缺status时判失败(self):
        self.assertFalse(judge({"类型": "业务状态", "等于": 0},
                               resp(json_body={"success": True}))[0])


class TestSuccessFlag(unittest.TestCase):

    def test_等于true(self):
        self.assertTrue(judge({"类型": "成功标志", "等于": True},
                              resp(json_body={"success": True}))[0])

    def test_等于false时实际为true应失败(self):
        ok, detail = judge({"类型": "成功标志", "等于": False},
                           resp(json_body={"success": True}))
        self.assertFalse(ok)
        self.assertIn("True", detail)

    def test_省略等于时默认要true(self):
        self.assertTrue(judge({"类型": "成功标志"}, resp(json_body={"success": True}))[0])
        self.assertFalse(judge({"类型": "成功标志"}, resp(json_body={"success": False}))[0])

    def test_字符串true不算成功(self):
        """服务端把布尔写成字符串是常见坑，必须判失败而不是模糊放过。"""
        self.assertFalse(judge({"类型": "成功标志", "等于": True},
                               resp(json_body={"success": "true"}))[0])

    def test_没有json时判失败(self):
        self.assertFalse(judge({"类型": "成功标志", "等于": True}, resp(json_body=None))[0])


class TestBodyText(unittest.TestCase):

    def test_包含_全部命中才算通过(self):
        r = resp(text='{"code":"OK","msg":"done"}')
        self.assertTrue(judge({"类型": "包含", "值": ["OK", "done"]}, r)[0])
        ok, detail = judge({"类型": "包含", "值": ["OK", "missing"]}, r)
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_不包含_命中任一即失败(self):
        r = resp(text='{"msg":"验证码错误"}')
        self.assertTrue(judge({"类型": "不包含", "值": "密码错误"}, r)[0])
        ok, detail = judge({"类型": "不包含", "值": ["密码错误", "验证码错误"]}, r)
        self.assertFalse(ok)
        self.assertIn("验证码错误", detail)

    def test_值可以是单个字符串而不是数组(self):
        self.assertTrue(judge({"类型": "包含", "值": "ok"}, resp(text="ok"))[0])

    def test_空响应体时包含判失败(self):
        self.assertFalse(judge({"类型": "包含", "值": "ok"}, resp(text=""))[0])


# ---------------------------------------------------------------- 字段层

class TestJsonField(unittest.TestCase):

    def test_非空_普通值(self):
        self.assertTrue(judge({"类型": "JSON字段", "路径": "a", "非空": True},
                              resp(json_body={"a": 1}))[0])

    def test_非空_零和false算有值(self):
        """0 和 false 是有效值，不能被当成"空"而误判成失败。"""
        self.assertTrue(judge({"类型": "JSON字段", "路径": "a", "非空": True},
                              resp(json_body={"a": 0}))[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "a", "非空": True},
                              resp(json_body={"a": False}))[0])

    def test_非空_空数组空对象空串不算有值(self):
        for empty in ([], {}, ""):
            with self.subTest(empty=empty):
                self.assertFalse(judge({"类型": "JSON字段", "路径": "a", "非空": True},
                                       resp(json_body={"a": empty}))[0])

    def test_为空(self):
        self.assertTrue(judge({"类型": "JSON字段", "路径": "a", "为空": True},
                              resp(json_body={"a": []}))[0])
        self.assertFalse(judge({"类型": "JSON字段", "路径": "a", "为空": True},
                               resp(json_body={"a": [1]}))[0])

    def test_等于_数字跨类型比较(self):
        self.assertTrue(judge({"类型": "JSON字段", "路径": "n", "等于": 5},
                              resp(json_body={"n": 5.0}))[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "n", "等于": 5},
                              resp(json_body={"n": "5"}))[0])

    def test_等于_字符串要求严格一致(self):
        self.assertFalse(judge({"类型": "JSON字段", "路径": "s", "等于": "abc"},
                               resp(json_body={"s": "abc "}))[0])

    def test_正则(self):
        self.assertTrue(judge({"类型": "JSON字段", "路径": "at", "正则": r"^eyJ"},
                              resp(json_body={"at": "eyJhbGci"}))[0])
        self.assertFalse(judge({"类型": "JSON字段", "路径": "at", "正则": r"^eyJ"},
                               resp(json_body={"at": "abc"}))[0])

    def test_对非字符串做正则判失败而不是崩(self):
        self.assertFalse(judge({"类型": "JSON字段", "路径": "n", "正则": r"\d"},
                               resp(json_body={"n": 123}))[0])

    def test_长度_数组与字符串与对象(self):
        r = resp(json_body={"arr": [1, 2, 3], "s": "abcd", "o": {"a": 1}})
        self.assertTrue(judge({"类型": "JSON字段", "路径": "arr", "长度": 3}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "s", "长度": 4}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "o", "长度": 1}, r)[0])

    def test_长度_对数字判失败(self):
        ok, detail = judge({"类型": "JSON字段", "路径": "n", "长度": 1},
                           resp(json_body={"n": 123}))
        self.assertFalse(ok)
        self.assertIn("None", detail)

    def test_长度_可用变量做自洽断言(self):
        """records 条数 = 同响应里的 total，是 v1.2 的核心用法。"""
        r = resp(json_body={"data": {"records": [1, 2, 3], "total": 3}})
        self.assertTrue(
            judge({"类型": "JSON字段", "路径": "data.records", "长度": "${total}"},
                  r, {"total": 3})[0])

    def test_长度_变量取不到时判失败并说明(self):
        r = resp(json_body={"data": {"records": [1, 2, 3]}})
        ok, detail = judge({"类型": "JSON字段", "路径": "data.records", "长度": "${total}"},
                           r, {})
        self.assertFalse(ok)
        self.assertIn("无法解析", detail)

    def test_大于_大于等于_小于等于(self):
        r = resp(json_body={"n": 10})
        self.assertTrue(judge({"类型": "JSON字段", "路径": "n", "大于": 5}, r)[0])
        self.assertFalse(judge({"类型": "JSON字段", "路径": "n", "大于": 10}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "n", "大于等于": 10}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "n", "小于等于": 10}, r)[0])
        self.assertFalse(judge({"类型": "JSON字段", "路径": "n", "小于等于": 9}, r)[0])

    def test_大小比较对非数字判失败(self):
        self.assertFalse(judge({"类型": "JSON字段", "路径": "n", "大于": 0},
                               resp(json_body={"n": "10"}))[0])

    def test_布尔值不能参与大小比较(self):
        """Python 里 True > 0 成立，但业务上"成功标志大于0"没有意义，应当判失败。"""
        self.assertFalse(judge({"类型": "JSON字段", "路径": "b", "大于": 0},
                               resp(json_body={"b": True}))[0])

    def test_期望在_区间闭区间(self):
        r = resp(json_body={"total": 5})
        self.assertTrue(judge({"类型": "JSON字段", "路径": "total", "期望在": [1, 10]}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "total", "期望在": [5, 5]}, r)[0])
        self.assertFalse(judge({"类型": "JSON字段", "路径": "total", "期望在": [6, 10]}, r)[0])

    def test_是数组_是布尔_是整数_是字符串(self):
        r = resp(json_body={"arr": [], "b": True, "i": 3, "s": "x"})
        self.assertTrue(judge({"类型": "JSON字段", "路径": "arr", "是数组": True}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "b", "是布尔": True}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "i", "是整数": True}, r)[0])
        self.assertTrue(judge({"类型": "JSON字段", "路径": "s", "是字符串": True}, r)[0])

    def test_是整数不把布尔当成整数(self):
        """Python 里 isinstance(True, int) 为真，这是最容易静默判错的一处。"""
        self.assertFalse(judge({"类型": "JSON字段", "路径": "b", "是整数": True},
                               resp(json_body={"b": True}))[0])

    def test_字段不存在时判失败(self):
        ok, detail = judge({"类型": "JSON字段", "路径": "nope", "非空": True},
                           resp(json_body={"a": 1}))
        self.assertFalse(ok)
        self.assertIn("nope", detail)

    def test_没有json时不崩(self):
        self.assertFalse(judge({"类型": "JSON字段", "路径": "a", "非空": True},
                               resp(json_body=None))[0])


class TestPathExists(unittest.TestCase):

    def test_存在(self):
        self.assertTrue(judge({"类型": "路径存在", "路径": "data.id"},
                              resp(json_body={"data": {"id": 1}}))[0])

    def test_缺失(self):
        ok, detail = judge({"类型": "路径存在", "路径": "data.id"},
                           resp(json_body={"data": {}}))
        self.assertFalse(ok)
        self.assertIn("缺失", detail)

    def test_数组下标路径(self):
        self.assertTrue(judge({"类型": "路径存在", "路径": "records.0.id"},
                              resp(json_body={"records": [{"id": 9}]}))[0])

    def test_显式null视作不存在(self):
        """JSON 里的 null 没有"值"，路径存在应当判失败。"""
        self.assertFalse(judge({"类型": "路径存在", "路径": "a"},
                               resp(json_body={"a": None}))[0])


# ---------------------------------------------------------------- 页面层

class TestUiGuards(unittest.TestCase):

    def test_页面断言挂在HTTP步骤上要明确报规格错误(self):
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "页面", "选择器": "h1", "可见": True}, resp())
        self.assertIn("只能挂在 UI 步骤", str(cm.exception))

    def test_选择器不在快照范围内要提示拼错可能(self):
        r = resp(ui=True, json_body=page({"h1": el()}))
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "页面", "选择器": "#nope", "存在": True}, r)
        self.assertIn("拼错", str(cm.exception))

    def test_浏览器不可用时报不可用而不是选择器不存在(self):
        r = resp(ui=True, json_body=page(), error="浏览器启动失败")
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "页面", "选择器": "h1", "存在": True}, r)
        self.assertIn("浏览器启动失败", str(cm.exception))

    def test_页面断言缺少选择器报规格错误(self):
        with self.assertRaises(AssertError):
            judge({"类型": "页面", "可见": True}, resp(ui=True, json_body=page()))


class TestPage(unittest.TestCase):

    def setUp(self):
        self.r = resp(ui=True, json_body=page({
            "h1": el(text="我的应用"),
            "input": el(value="zrgj"),
            "btn": el(enabled=False),
            "list tr": el(count=3),
            "img": el(count=0, exists=False, visible=False),
            "a": el(attrs={"href": "/x", "target": "_blank"}),
        }))

    def test_存在(self):
        self.assertTrue(judge({"类型": "页面", "选择器": "h1", "存在": True}, self.r)[0])
        self.assertFalse(judge({"类型": "页面", "选择器": "img", "存在": True}, self.r)[0])
        self.assertTrue(judge({"类型": "页面", "选择器": "img", "存在": False}, self.r)[0])

    def test_探测失败时拒绝判定而不是判不存在(self):
        """探测抛异常（页面在跳转 / iframe 已卸载）≠ 元素不存在。

        两者在报告里长得一样，结论却相反：一个是产品缺陷，一个是测试时机不对。
        """
        r = resp(ui=True, json_body=page({
            "h1": {"count": None, "exists": None, "probe_failed": True,
                   "error": "RuntimeError: Execution context was destroyed"},
        }))
        with self.assertRaises(AssertError) as ctx:
            judge({"类型": "页面", "选择器": "h1", "存在": True}, r)
        msg = str(ctx.exception)
        self.assertIn("无法判定", msg)
        self.assertIn("Execution context", msg)
        self.assertIn("不是元素不存在", msg)

    def test_探测失败对每个判定条件都生效(self):
        # 不只是「存在」，文本/数量/可见 也一样不能拿 probe_failed 去判
        r = resp(ui=True, json_body=page({
            "h1": {"count": None, "exists": None, "probe_failed": True, "error": "boom"},
        }))
        for cond in ({"可见": True}, {"数量": 1}, {"文本": "x"}, {"值": "x"}):
            cond = dict(cond, **{"类型": "页面", "选择器": "h1"})
            with self.assertRaises(AssertError):
                judge(cond, r)

    def test_确实不存在仍然判不存在(self):
        # 守卫不能把「真的没有」也变成「无法判定」，否则缺失元素再也不会被发现
        self.assertTrue(judge({"类型": "页面", "选择器": "img", "存在": False}, self.r)[0])

    def test_可见(self):
        self.assertTrue(judge({"类型": "页面", "选择器": "h1", "可见": True}, self.r)[0])
        self.assertFalse(judge({"类型": "页面", "选择器": "img", "可见": True}, self.r)[0])

    def test_数量(self):
        self.assertTrue(judge({"类型": "页面", "选择器": "list tr", "数量": 3}, self.r)[0])
        self.assertFalse(judge({"类型": "页面", "选择器": "list tr", "数量": 2}, self.r)[0])

    def test_数量可用变量(self):
        self.assertTrue(
            judge({"类型": "页面", "选择器": "list tr", "数量": "${n}"},
                  self.r, {"n": 3})[0])

    def test_文本是包含语义(self):
        self.assertTrue(judge({"类型": "页面", "选择器": "h1", "文本": "我的"}, self.r)[0])
        self.assertFalse(judge({"类型": "页面", "选择器": "h1", "文本": "你的"}, self.r)[0])

    def test_文本等于忽略首尾空白(self):
        self.assertTrue(
            judge({"类型": "页面", "选择器": "h1", "文本等于": " 我的应用 "}, self.r)[0])
        self.assertFalse(
            judge({"类型": "页面", "选择器": "h1", "文本等于": "我的"}, self.r)[0])

    def test_不含文本(self):
        self.assertTrue(
            judge({"类型": "页面", "选择器": "h1", "不含文本": "错误"}, self.r)[0])
        self.assertFalse(
            judge({"类型": "页面", "选择器": "h1", "不含文本": ["我的"]}, self.r)[0])

    def test_值(self):
        self.assertTrue(judge({"类型": "页面", "选择器": "input", "值": "zrgj"}, self.r)[0])
        self.assertFalse(judge({"类型": "页面", "选择器": "input", "值": "other"}, self.r)[0])

    def test_值可为空串(self):
        r = resp(ui=True, json_body=page({"input": el(value=None)}))
        self.assertTrue(judge({"类型": "页面", "选择器": "input", "值": ""}, r)[0])

    def test_可用(self):
        self.assertFalse(judge({"类型": "页面", "选择器": "btn", "可用": True}, self.r)[0])
        self.assertTrue(judge({"类型": "页面", "选择器": "btn", "可用": False}, self.r)[0])

    def test_属性子集匹配(self):
        self.assertTrue(
            judge({"类型": "页面", "选择器": "a", "属性": {"href": "/x"}}, self.r)[0])
        self.assertFalse(
            judge({"类型": "页面", "选择器": "a", "属性": {"href": "/y"}}, self.r)[0])


class TestUrl(unittest.TestCase):
    """URL 断言只认 等于/包含/不含/正则 —— 写成「不包含」会变成规格错误。

    2026-10-20 有个 UI 用例正是因为把键写成「不包含」，报「URL 断言需指定…」，
    在报告里表现为"登录失败"，被误读成验证码过不去。这组测试把这条钉住。
    """

    def setUp(self):
        self.r = resp(ui=True, url="http://h/application/myapplication",
                      json_body=page(url="http://h/application/myapplication"))

    def test_等于(self):
        self.assertTrue(
            judge({"类型": "URL", "等于": "http://h/application/myapplication"}, self.r)[0])

    def test_包含(self):
        self.assertTrue(judge({"类型": "URL", "包含": "/application/"}, self.r)[0])

    def test_不含(self):
        self.assertTrue(judge({"类型": "URL", "不含": "/login"}, self.r)[0])
        self.assertFalse(judge({"类型": "URL", "不含": "myapplication"}, self.r)[0])

    def test_正则(self):
        self.assertTrue(judge({"类型": "URL", "正则": r"/application/\w+"}, self.r)[0])

    def test_错写成不包含要报规格错误(self):
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "URL", "不包含": "/login"}, self.r)
        self.assertIn("不含", str(cm.exception))

    def test_页面快照缺url时回落到响应url(self):
        r = resp(ui=True, url="http://h/x", json_body={"elements": {}})
        self.assertTrue(judge({"类型": "URL", "包含": "/x"}, r)[0])


class TestConsole(unittest.TestCase):

    def test_无错误_通过(self):
        self.assertTrue(
            judge({"类型": "控制台", "无错误": True},
                  resp(ui=True, json_body=page()))[0])

    def test_有错误时失败并列条数(self):
        r = resp(ui=True, json_body=page(console_errors=["Uncaught TypeError: x"]))
        ok, detail = judge({"类型": "控制台", "无错误": True}, r)
        self.assertFalse(ok)
        self.assertIn("1 条", detail)

    def test_页面级报错也算错误(self):
        """console 与 pageerror 是两条采集通道，只合并才算完整。"""
        r = resp(ui=True, json_body=page(page_errors=["ReferenceError: y"]))
        self.assertFalse(judge({"类型": "控制台", "无错误": True}, r)[0])

    def test_不含_命中禁用片段(self):
        r = resp(ui=True, json_body=page(console_errors=["Warning: Minified React error #418"]))
        self.assertFalse(judge({"类型": "控制台", "不含": "React error"}, r)[0])
        self.assertTrue(judge({"类型": "控制台", "不含": "TypeError"}, r)[0])

    def test_期望有错误时可反向断言(self):
        r = resp(ui=True, json_body=page(console_errors=["boom"]))
        self.assertTrue(judge({"类型": "控制台", "无错误": False}, r)[0])


class TestNetwork(unittest.TestCase):

    def test_无失败请求_通过(self):
        self.assertTrue(
            judge({"类型": "网络", "无失败请求": True},
                  resp(ui=True, json_body=page()))[0])

    def test_有失败请求时失败并列出明细(self):
        fails = [{"method": "GET", "status": 404, "url": "http://h/missing.js"}]
        ok, detail = judge({"类型": "网络", "无失败请求": True},
                           resp(ui=True, json_body=page(network_failures=fails)))
        self.assertFalse(ok)
        self.assertIn("404", detail)

    def test_允许白名单内的失败请求不进统计(self):
        fails = [{"method": "GET", "status": 404, "url": "http://h/favicon.ico"}]
        ok, _ = judge({"类型": "网络", "无失败请求": True, "允许": ["favicon"]},
                      resp(ui=True, json_body=page(network_failures=fails)))
        self.assertTrue(ok)

    def test_白名单只挡住匹配项其余仍算失败(self):
        fails = [{"method": "GET", "status": 404, "url": "http://h/favicon.ico"},
                 {"method": "GET", "status": 500, "url": "http://h/api/list"}]
        ok, detail = judge({"类型": "网络", "无失败请求": True, "允许": ["favicon"]},
                           resp(ui=True, json_body=page(network_failures=fails)))
        self.assertFalse(ok)
        self.assertIn("api/list", detail)


class TestNetworkRequestCount(unittest.TestCase):
    """「网络」的请求级断言：调用次数、请求体内容。

    这类断言最容易假通过（统计口径写错就永远为真），所以每个分支都钉死。
    """

    def _r(self, reqs, truncated=False):
        return resp(ui=True, json_body=page(network_requests=reqs,
                                            network_truncated=truncated))

    def test_次数等于通过(self):
        ok, detail = judge({"类型": "网络", "请求": "/api/order/submit", "次数": 1},
                           self._r([req("http://h/api/order/submit", "POST")]))
        self.assertTrue(ok, detail)
        self.assertIn("1 次", detail)

    def test_次数不符时说明实际次数(self):
        ok, detail = judge({"类型": "网络", "请求": "/api/order/submit", "次数": 1},
                           self._r([req("http://h/api/order/submit"),
                                    req("http://h/api/order/submit")]))
        self.assertFalse(ok)
        self.assertIn("2 次", detail)

    def test_次数支持范围条件(self):
        r = self._r([req("http://h/api/user/list"), req("http://h/api/user/list")])
        self.assertTrue(judge({"类型": "网络", "请求": "/api/user/list",
                               "次数": {"小于等于": 2}}, r)[0])
        self.assertFalse(judge({"类型": "网络", "请求": "/api/user/list",
                                "次数": {"小于等于": 1}}, r)[0])
        self.assertTrue(judge({"类型": "网络", "请求": "/api/user/list",
                               "次数": {"大于等于": 1}}, r)[0])

    def test_方法过滤只数指定方法(self):
        r = self._r([req("http://h/api/x", "GET"), req("http://h/api/x", "POST")])
        self.assertTrue(judge({"类型": "网络", "请求": "/api/x", "方法": "post",
                               "次数": 1}, r)[0])
        self.assertFalse(judge({"类型": "网络", "请求": "/api/x", "方法": "POST",
                                "次数": 2}, r)[0])

    def test_没匹配到请求时次数0可以判定(self):
        ok, detail = judge({"类型": "网络", "请求": "/api/never", "次数": 0},
                           self._r([req("http://h/api/other")]))
        self.assertTrue(ok, detail)

    def test_请求体包含通过(self):
        r = self._r([req("http://h/api/order/submit", "POST",
                         body='{"source":"h5","amount":1}')])
        ok, _ = judge({"类型": "网络", "请求": "/api/order/submit", "方法": "POST",
                       "请求体包含": ["\"source\":\"h5\"", "amount"]}, r)
        self.assertTrue(ok)

    def test_请求体缺内容时列出实际体(self):
        r = self._r([req("http://h/api/order/submit", "POST", body='{"source":"pc"}')])
        ok, detail = judge({"类型": "网络", "请求": "/api/order/submit",
                            "请求体包含": "\"source\":\"h5\""}, r)
        self.assertFalse(ok)
        self.assertIn("source", detail)

    def test_没有匹配请求时请求体断言失败而不是通过(self):
        ok, detail = judge({"类型": "网络", "请求": "/api/order/submit",
                            "请求体包含": "x"}, self._r([req("http://h/api/other")]))
        self.assertFalse(ok)
        self.assertIn("没有匹配", detail)

    def test_记录被截断时拒绝判定(self):
        # 截断后的统计必错。这里必须抛规格错误（无法判定），不能判通过也不能判失败。
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "网络", "请求": "/api/x", "次数": 1},
                  self._r([req("http://h/api/x")], truncated=True))
        self.assertIn("截断", str(cm.exception))

    def test_旧结果没有请求明细时拒绝判定(self):
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "网络", "请求": "/api/x", "次数": 1},
                  resp(ui=True, json_body=page()))
        self.assertIn("network_requests", str(cm.exception))

    def test_给了请求但没给判定条件报规格错误(self):
        with self.assertRaises(AssertError) as cm:
            judge({"类型": "网络", "请求": "/api/x"}, self._r([req("http://h/api/x")]))
        self.assertIn("判定条件", str(cm.exception))


# ---------------------------------------------------------------- 变量插值

class TestFill(unittest.TestCase):

    def test_单变量替换(self):
        self.assertEqual(A.fill("${a}", {"a": 1}), "1")

    def test_多变量与部分文本(self):
        self.assertEqual(A.fill("id=${a}&n=${b}", {"a": 1, "b": 2}), "id=1&n=2")

    def test_未定义变量原样保留(self):
        """保留原文而不是替换成空串，这样断言会明确失败并显示 ${total}，
        比静默变成空字符串更容易定位「哪一步提取没生效」。"""
        self.assertEqual(A.fill("${nope}", {}), "${nope}")

    def test_非字符串原样返回(self):
        self.assertEqual(A.fill(5, {}), 5)
        self.assertIsNone(A.fill(None, {}))

    def test_列表与字典递归替换(self):
        self.assertEqual(A.fill(["${a}", {"k": "${a}"}], {"a": "v"}),
                         ["v", {"k": "v"}])

    def test_支持env前缀与点号键(self):
        self.assertEqual(A.fill("${env.TOKEN}", {"env.TOKEN": "t"}), "t")


class TestDeepGet(unittest.TestCase):

    def test_普通路径(self):
        self.assertEqual(A.deep_get({"a": {"b": 1}}, "a.b"), 1)

    def test_数组下标(self):
        self.assertEqual(A.deep_get({"a": [{"b": 2}]}, "a.0.b"), 2)

    def test_下标越界返回None(self):
        self.assertIsNone(A.deep_get({"a": [1]}, "a.9"))

    def test_非数字下标返回None(self):
        self.assertIsNone(A.deep_get({"a": [1]}, "a.x"))

    def test_中间层为None返回None(self):
        self.assertIsNone(A.deep_get({"a": None}, "a.b"))

    def test_中间层是标量返回None(self):
        self.assertIsNone(A.deep_get({"a": 1}, "a.b"))

    def test_空路径返回原对象(self):
        self.assertEqual(A.deep_get({"a": 1}, ""), {"a": 1})


# ---------------------------------------------------------------- 静态校验

class TestCheckShape(unittest.TestCase):
    """lint / compile 用的静态校验。它拦不住的写法，就得靠运行时兜住。"""

    def test_合法的断言没有问题(self):
        self.assertEqual(
            A.check_assertion_shape({"类型": "状态码", "等于": 200}), [])

    def test_未知类型被拦下(self):
        self.assertTrue(A.check_assertion_shape({"类型": "数据库"}))

    def test_JSON字段缺路径被拦下(self):
        problems = A.check_assertion_shape({"类型": "JSON字段", "非空": True})
        self.assertTrue(any("路径" in p for p in problems))

    def test_响应头缺名称被拦下(self):
        problems = A.check_assertion_shape({"类型": "响应头", "包含": "json"})
        self.assertTrue(any("名称" in p for p in problems))

    def test_页面只给选择器被拦下(self):
        problems = A.check_assertion_shape({"类型": "页面", "选择器": "h1"})
        self.assertTrue(any("判定条件" in p for p in problems))

    def test_引用未定义变量被拦下(self):
        problems = A.check_assertion_shape(
            {"类型": "JSON字段", "路径": "a", "等于": "${total}"})
        self.assertTrue(any("${total}" in p for p in problems))

    def test_变量在已提取集合里则放行(self):
        self.assertEqual(
            A.check_assertion_shape(
                {"类型": "JSON字段", "路径": "a", "等于": "${total}"},
                available_vars={"total"}), [])

    def test_env变量不需要事先提取(self):
        self.assertEqual(
            A.check_assertion_shape(
                {"类型": "状态码", "等于": "${env.STATUS}"}), [])

    def test_期望在不是二元组被拦下(self):
        problems = A.check_assertion_shape(
            {"类型": "JSON字段", "路径": "a", "期望在": 5})
        self.assertTrue(any("期望在" in p for p in problems))

    def test_网络给了请求但没给判定条件被拦下(self):
        problems = A.check_assertion_shape({"类型": "网络", "请求": "/api/x"})
        self.assertTrue(any("判定条件" in p for p in problems))

    def test_网络两个分支都没给被拦下(self):
        problems = A.check_assertion_shape({"类型": "网络"})
        self.assertTrue(any("无失败请求" in p for p in problems))

    def test_网络次数条件写错被拦下(self):
        problems = A.check_assertion_shape(
            {"类型": "网络", "请求": "/api/x", "次数": {"约等于": 1}})
        self.assertTrue(any("次数" in p for p in problems))

    def test_网络次数写整数是合法的(self):
        self.assertEqual(
            A.check_assertion_shape({"类型": "网络", "请求": "/api/x", "次数": 1}), [])


# ---------------------------------------------------------------- 下载产物

class TestDownload(unittest.TestCase):
    """「下载」断言判的是磁盘上真实存在的文件。

    这一类的信任成本最高：断言通过意味着「用户确实拿到了这个文件」，
    所以判不了的情况一律要拦，不能默认通过。
    """

    def test_后缀匹配(self):
        ok, detail = judge({"类型": "下载", "后缀": ".xlsx"},
                           resp(downloads=[artifact()]))
        self.assertTrue(ok)
        self.assertIn("导出.xlsx", detail)
        self.assertIn(".xlsx", detail)

    def test_后缀不匹配报出实际后缀(self):
        ok, detail = judge({"类型": "下载", "后缀": ".csv"},
                           resp(downloads=[artifact()]))
        self.assertFalse(ok)
        self.assertIn(".xlsx", detail)

    def test_后缀不带点也认(self):
        ok, _ = judge({"类型": "下载", "后缀": "xlsx"},
                      resp(downloads=[artifact()]))
        self.assertTrue(ok)

    def test_后缀大小写不敏感(self):
        ok, _ = judge({"类型": "下载", "后缀": ".XLSX"},
                      resp(downloads=[artifact()]))
        self.assertTrue(ok)

    def test_无后缀文件说明里明确写无(self):
        ok, detail = judge({"类型": "下载", "后缀": ".xlsx"},
                           resp(downloads=[artifact(name="report")]))
        self.assertFalse(ok)
        self.assertIn("无", detail)

    def test_最小字节边界等于算通过(self):
        ok, _ = judge({"类型": "下载", "最小字节": 20480},
                      resp(downloads=[artifact(size=20480)]))
        self.assertTrue(ok)

    def test_最小字节差一字节就失败(self):
        ok, _ = judge({"类型": "下载", "最小字节": 20481},
                      resp(downloads=[artifact(size=20480)]))
        self.assertFalse(ok)

    def test_最大字节(self):
        self.assertTrue(judge({"类型": "下载", "最大字节": 20480},
                              resp(downloads=[artifact(size=20480)]))[0])
        self.assertFalse(judge({"类型": "下载", "最大字节": 20479},
                               resp(downloads=[artifact(size=20480)]))[0])

    def test_名称包含(self):
        ok, _ = judge({"类型": "下载", "名称包含": "导出"},
                      resp(downloads=[artifact()]))
        self.assertTrue(ok)

    def test_数量整数(self):
        ok, _ = judge({"类型": "下载", "数量": 2},
                      resp(downloads=[artifact(), artifact(name="b.xlsx")]))
        self.assertTrue(ok)
        self.assertFalse(judge({"类型": "下载", "数量": 1},
                               resp(downloads=[artifact(), artifact()]))[0])

    def test_数量条件写法(self):
        ok, _ = judge({"类型": "下载", "数量": {"大于等于": 1}},
                      resp(downloads=[artifact()]))
        self.assertTrue(ok)

    def test_数量不符时说明实际个数(self):
        ok, detail = judge({"类型": "下载", "数量": 3},
                           resp(downloads=[artifact()]))
        self.assertFalse(ok)
        self.assertIn("1 个", detail)

    def test_多条条件全部成立才通过(self):
        ok, detail = judge({"类型": "下载", "后缀": ".xlsx", "最小字节": 999999},
                           resp(downloads=[artifact()]))
        self.assertFalse(ok)
        self.assertIn("≥999999", detail.replace(",", ""))

    def test_没有下载记录是规格错误(self):
        with self.assertRaises(A.AssertError) as ctx:
            judge({"类型": "下载", "后缀": ".xlsx"}, resp())
        self.assertIn("下载目录", str(ctx.exception))

    def test_声明了下载但一个文件都没有(self):
        ok, detail = judge({"类型": "下载", "后缀": ".xlsx"}, resp(downloads=[]))
        self.assertFalse(ok)
        self.assertIn("没有收到下载", detail)

    def test_超限截断拒绝判定(self):
        with self.assertRaises(A.AssertError) as ctx:
            judge({"类型": "下载", "最小字节": 1},
                  resp(downloads=[artifact(size=999, path="", truncated=True)]))
        self.assertIn("无法判定", str(ctx.exception))

    def test_保存失败拒绝判定(self):
        with self.assertRaises(A.AssertError) as ctx:
            judge({"类型": "下载", "后缀": ".xlsx"},
                  resp(downloads=[artifact(path="", error="IOError: 磁盘满")]))
        self.assertIn("保存失败", str(ctx.exception))

    def test_存在要真的查磁盘(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "real.xlsx")
        with open(p, "wb") as f:
            f.write(b"x" * 10)
        self.assertTrue(judge({"类型": "下载", "存在": True},
                              resp(downloads=[artifact(path=p)]))[0])
        missing = os.path.join(d, "gone.xlsx")
        ok, detail = judge({"类型": "下载", "存在": True},
                           resp(downloads=[artifact(path=missing)]))
        self.assertFalse(ok)
        self.assertIn("未落盘", detail)

    def test_存在False可以断言不该有产物(self):
        self.assertTrue(judge({"类型": "下载", "存在": False},
                              resp(downloads=[artifact(path="/nope/x")]))[0])

    def test_没有判定条件是规格错误(self):
        with self.assertRaises(A.AssertError) as ctx:
            judge({"类型": "下载"}, resp(downloads=[artifact()]))
        self.assertIn("至少要有", str(ctx.exception))

    def test_浏览器通道不可用时拒绝判定(self):
        r = resp(downloads=[])
        r["ui_unavailable"] = True
        with self.assertRaises(A.AssertError) as ctx:
            judge({"类型": "下载", "后缀": ".xlsx"}, r)
        self.assertIn("浏览器通道不可用", str(ctx.exception))

    def test_多处下载取最后一个(self):
        # 同一个用例里先下模板再下数据，「后缀」应该判最后那份
        ok, detail = judge(
            {"类型": "下载", "后缀": ".csv"},
            resp(downloads=[artifact(name="模板.xlsx"),
                            artifact(name="数据.csv")]))
        self.assertTrue(ok)
        self.assertIn("数据.csv", detail)

    def test_形状校验拦住非数字字节数(self):
        problems = A.check_assertion_shape({"类型": "下载", "最小字节": "很多"})
        self.assertTrue(any("最小字节" in p for p in problems))

    def test_形状校验放过合法写法(self):
        self.assertEqual(
            A.check_assertion_shape({"类型": "下载", "后缀": ".pdf", "最小字节": 10}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
