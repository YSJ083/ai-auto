"""ui.py 的单元测试 —— 不启动浏览器、不联网、毫秒级。

为什么能不用真浏览器：
`run_ui_step()` 只依赖 session 的五个接口（goto / act / settle / snapshot / url），
把它换成一个假对象，status、precheck、错误分类这些**最容易错也最影响结论**的
组装逻辑就都能测。真浏览器只用来验证「动作能不能点对地方」，那是集成测试的事。

这里重点钉住三条曾经踩过的坑：
  1. 没导航的 UI 步骤不能返回 200 —— 否则「状态码 200」断言恒为真
  2. 导航失败（连不上）与动作失败（元素找不到）要能分开，前者算环境、后者算缺陷
  3. 探测失败不能写成 exists=False —— 那会把「页面在跳转」报成「元素没渲染」

运行：
    cd ~/.workbuddy/skills/ai-auto-test
    python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import ui                     # noqa: E402
from core.ui import UiError            # noqa: E402


# ---------------------------------------------------------------- 假会话

class FakeSession(object):
    """只实现 run_ui_step 用到的五个接口，其余一概不管。"""

    def __init__(self, nav_status=200, goto_error="", act_error="",
                 snapshot=None, url="http://host/current"):
        self.nav_status = nav_status
        self.goto_error = goto_error
        self.act_error = act_error
        self.snap = snapshot if snapshot is not None else {
            "url": url, "title": "", "text": "页面文本", "elements": {},
            "console_errors": [], "page_errors": [], "network_failures": [],
            "diagnostics": [],
        }
        self.url = url
        self.selectors = []
        self.need_text = True
        self.calls = []
        self.downloads = []
        self.uploads = []

    def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(("goto", url))
        if self.goto_error:
            raise UiError("打开页面失败：%s（%s）" % (url, self.goto_error))
        return self.nav_status

    def settle(self, step, after_nav=False):
        self.calls.append(("settle", after_nav))

    def act(self, step, variables):
        self.calls.append(("act", step.get("ui") or step.get("动作")))
        if self.act_error:
            raise UiError(self.act_error)

    def snapshot(self, selectors=None, need_text=None, net_since=None,
                 dl_since=None, up_since=None):
        # 三个窗口参数必须显式收下：写成 **kw 会把 run_ui_step 传错参数名这件事
        # 悄悄吞掉，测试就再也测不出「窗口没生效」这类问题了。
        self.last_windows = (net_since, dl_since, up_since)
        return dict(self.snap)


def run(step, session=None, base="http://host"):
    s = session or FakeSession()
    return ui.run_ui_step(s, step, base, {}), s


# ---------------------------------------------------------------- status 语义

class TestStatusSemantics(unittest.TestCase):
    """UI 步骤的 status 必须能区分「导航过」和「没导航」。"""

    def test_no_navigation_returns_none_not_200(self):
        # 这条是整个改动的核心：以前返回 200，导致挂在点击步骤上的
        # {"类型":"状态码","等于":200} 永远通过，等于没写断言。
        resp, _ = run({"ui": "点击", "选择器": "button"})
        self.assertIsNone(resp["status"])

    def test_no_navigation_success_has_no_error(self):
        resp, _ = run({"ui": "点击", "选择器": "button"})
        self.assertEqual(resp["error"], "")
        self.assertFalse(resp["ui_action_failed"])
        self.assertFalse(resp["ui_nav_failed"])

    def test_navigation_reports_document_status(self):
        resp, _ = run({"url": "/home", "ui": "断言快照"})
        self.assertEqual(resp["status"], 200)

    def test_navigation_500_is_surfaced(self):
        resp, _ = run({"url": "/boom", "ui": "断言快照"},
                      FakeSession(nav_status=500))
        self.assertEqual(resp["status"], 500)

    def test_action_error_without_navigation_is_zero(self):
        resp, _ = run({"ui": "点击", "选择器": "button"},
                      FakeSession(act_error="元素不存在"))
        self.assertEqual(resp["status"], 0)
        self.assertTrue(resp["ui_action_failed"])
        self.assertFalse(resp["ui_nav_failed"])

    def test_navigation_failure_is_flagged_separately(self):
        # 连不上属于环境问题，不能被算成「页面行为不符」
        resp, _ = run({"url": "/home", "ui": "断言快照"},
                      FakeSession(goto_error="net::ERR_CONNECTION_REFUSED"))
        self.assertEqual(resp["status"], 0)
        self.assertTrue(resp["ui_nav_failed"])
        self.assertFalse(resp["ui_action_failed"])

    def test_action_error_after_successful_navigation_keeps_nav_status(self):
        # 导航成功了但动作失败：状态码仍是 200，失败靠 ui_action_failed 表达
        resp, _ = run({"url": "/home", "ui": "点击", "选择器": "x"},
                      FakeSession(nav_status=200, act_error="点击超时"))
        self.assertEqual(resp["status"], 200)
        self.assertTrue(resp["ui_action_failed"])


# ---------------------------------------------------------------- precheck

class TestPrecheck(unittest.TestCase):

    def test_404_navigation_generates_precheck(self):
        resp, _ = run({"url": "/missing", "ui": "断言快照"},
                      FakeSession(nav_status=404))
        self.assertEqual(len(resp["precheck"]), 1)
        self.assertFalse(resp["precheck"][0]["passed"])
        self.assertIn("404", resp["precheck"][0]["actual"])

    def test_404_with_declared_status_assertion_skips_precheck(self):
        # 用例自己写了状态码断言，就交给它判，避免同一条结论报两遍
        step = {"url": "/missing", "ui": "断言快照",
                "断言": [{"类型": "状态码", "等于": 404}]}
        resp, _ = run(step, FakeSession(nav_status=404))
        self.assertEqual(resp["precheck"], [])

    def test_2xx_navigation_has_no_precheck(self):
        resp, _ = run({"url": "/home", "ui": "断言快照"},
                      FakeSession(nav_status=200))
        self.assertEqual(resp["precheck"], [])

    def test_no_navigation_has_no_precheck(self):
        resp, _ = run({"ui": "点击", "选择器": "b"})
        self.assertEqual(resp["precheck"], [])


# ---------------------------------------------------------------- URL 拼接

class TestNavigationTarget(unittest.TestCase):

    def test_relative_path_is_joined_with_origin(self):
        # "/login" 直接交给 Playwright 会失败并把页面留在 about:blank
        _, s = run({"url": "/login", "ui": "断言快照"}, base="http://10.0.0.1:8080")
        self.assertEqual(s.calls[0], ("goto", "http://10.0.0.1:8080/login"))

    def test_absolute_url_is_kept(self):
        _, s = run({"url": "https://other.example/x", "ui": "断言快照"},
                   base="http://10.0.0.1:8080")
        self.assertEqual(s.calls[0], ("goto", "https://other.example/x"))

    def test_path_field_also_works(self):
        _, s = run({"path": "/p", "ui": "断言快照"}, base="http://h:1")
        self.assertEqual(s.calls[0], ("goto", "http://h:1/p"))

    def test_navigation_settles_with_after_nav(self):
        _, s = run({"url": "/home", "ui": "断言快照"})
        self.assertIn(("settle", True), s.calls)

    def test_action_without_navigation_settles_normally(self):
        _, s = run({"ui": "点击", "选择器": "b"})
        self.assertIn(("settle", False), s.calls)


# ---------------------------------------------------------------- 诊断透传

class TestDiagnostics(unittest.TestCase):

    def test_diagnostics_are_passed_through(self):
        snap = {"url": "http://h/", "text": "", "elements": {},
                "diagnostics": [{"what": "失败截图没能保存（a.png）",
                                 "error": "OSError: disk full"}]}
        resp, _ = run({"ui": "断言快照"}, FakeSession(snapshot=snap))
        self.assertEqual(len(resp["diagnostics"]), 1)
        self.assertIn("disk full", resp["diagnostics"][0]["error"])

    def test_missing_diagnostics_is_empty_list(self):
        resp, _ = run({"ui": "断言快照"},
                      FakeSession(snapshot={"url": "http://h/", "text": "", "elements": {}}))
        self.assertEqual(resp["diagnostics"], [])

    def test_error_is_appended_to_response_text(self):
        resp, _ = run({"ui": "点击", "选择器": "x"},
                      FakeSession(act_error="元素不存在"))
        self.assertIn("[执行错误]", resp["text"])
        self.assertIn("元素不存在", resp["text"])


# ---------------------------------------------------------------- 响应同形

class TestResponseShape(unittest.TestCase):
    """UI 步骤必须与 HTTP 步骤同形，否则 runner / report / compare 要各改一遍。"""

    REQUIRED = ("url", "method", "status", "elapsed_ms", "json", "text", "headers")

    def test_has_http_shape_keys(self):
        resp, _ = run({"ui": "断言快照"})
        for k in self.REQUIRED:
            self.assertIn(k, resp)

    def test_method_is_prefixed_with_ui(self):
        resp, _ = run({"ui": "点击", "选择器": "b"})
        self.assertEqual(resp["method"], "UI:点击")

    def test_ui_step_is_marked(self):
        resp, _ = run({"ui": "断言快照"})
        self.assertTrue(resp["ui"])

    def test_elapsed_is_integer(self):
        resp, _ = run({"ui": "断言快照"})
        self.assertIsInstance(resp["elapsed_ms"], int)


# ---------------------------------------------------------------- 探测失败

class _BoomPage(object):
    """任何 locator 调用都炸掉，模拟「页面正在跳转 / frame 已卸载」。"""

    url = "http://host/broken"

    def locator(self, sel):
        raise RuntimeError("Execution context was destroyed")


class _EmptyPage(object):
    url = "http://host/empty"

    def __init__(self):
        self.inner_text_calls = 0
        self.locator_calls = []

    def title(self):
        return "标题"

    def inner_text(self, sel):
        self.inner_text_calls += 1
        return "整页文本"

    def locator(self, sel):
        self.locator_calls.append(sel)
        return _ZeroLocator()


class _ZeroLocator(object):
    def count(self):
        return 0


class TestProbe(unittest.TestCase):

    def test_probe_failure_is_not_reported_as_absent(self):
        s = ui.UiSession()
        s._page = _BoomPage()
        info = s._probe(".x")
        self.assertTrue(info["probe_failed"])
        self.assertIsNone(info["exists"])
        self.assertIsNone(info["count"])
        self.assertIn("Execution context", info["error"])

    def test_zero_match_is_genuinely_absent(self):
        s = ui.UiSession()
        s._page = _EmptyPage()
        info = s._probe(".x")
        self.assertEqual(info["exists"], False)
        self.assertEqual(info["count"], 0)
        self.assertNotIn("probe_failed", info)


# ---------------------------------------------------------------- 快照分级

class TestSnapshotLevel(unittest.TestCase):

    def test_light_snapshot_skips_body_text(self):
        p = _EmptyPage()
        s = ui.UiSession()
        s._page = p
        s.need_text = False
        snap = s.snapshot()
        self.assertEqual(snap["text"], "")
        self.assertEqual(p.inner_text_calls, 0)

    def test_full_snapshot_reads_body_text(self):
        p = _EmptyPage()
        s = ui.UiSession()
        s._page = p
        s.need_text = True
        snap = s.snapshot()
        self.assertEqual(snap["text"], "整页文本")
        self.assertEqual(p.inner_text_calls, 1)

    def test_explicit_need_text_overrides_session_default(self):
        p = _EmptyPage()
        s = ui.UiSession()
        s._page = p
        s.need_text = True
        s.snapshot(need_text=False)
        self.assertEqual(p.inner_text_calls, 0)

    def test_snapshot_carries_diagnostics(self):
        s = ui.UiSession()
        s._page = _EmptyPage()
        s._diag("测试诊断")
        snap = s.snapshot()
        self.assertEqual(snap["diagnostics"][0]["what"], "测试诊断")


class TestNeedsBodyText(unittest.TestCase):
    """判定宽了只会慢一点，判定窄了会让文本断言拿到空串 —— 从通过变失败。"""

    def test_no_assertion_does_not_need_text(self):
        self.assertFalse(ui.needs_body_text({
            "steps": [{"ui": "点击", "选择器": "b"}]}))

    def test_element_only_assertion_does_not_need_text(self):
        self.assertFalse(ui.needs_body_text({
            "steps": [{"ui": "断言快照", "断言": [{"类型": "页面", "选择器": "a", "存在": True}]}]}))

    def test_page_text_assertion_needs_text(self):
        self.assertTrue(ui.needs_body_text({
            "steps": [{"ui": "断言快照",
                       "断言": [{"类型": "页面", "选择器": "a", "文本": "首页"}]}]}))

    def test_contains_assertion_needs_text(self):
        # 「包含」对 UI 步骤比的就是 resp["text"]
        self.assertTrue(ui.needs_body_text({
            "steps": [{"ui": "点击", "选择器": "b", "断言": [{"类型": "包含", "值": "成功"}]}]}))

    def test_not_contains_assertion_needs_text(self):
        self.assertTrue(ui.needs_body_text({
            "steps": [{"ui": "点击", "选择器": "b",
                       "断言": [{"类型": "不包含", "值": "错误"}]}]}))

    def test_setup_text_assertion_needs_text(self):
        self.assertTrue(ui.needs_body_text({
            "steps": [{"ui": "点击"}],
            "setup": [{"ui": "断言快照", "断言": [{"类型": "页面", "选择器": "a", "不含文本": "错"}]}]}))

    def test_empty_case(self):
        self.assertFalse(ui.needs_body_text({}))


# ---------------------------------------------------------------- 选择器收集

class TestCollectSelectors(unittest.TestCase):

    def test_collects_from_steps_and_assertions(self):
        got = ui.collect_selectors({
            "steps": [{"ui": "点击", "选择器": "button.submit"},
                      {"ui": "断言快照", "断言": [{"类型": "页面", "选择器": ".msg", "存在": True}]}],
        })
        self.assertEqual(got, ["button.submit", ".msg"])

    def test_collects_from_setup_and_teardown(self):
        got = ui.collect_selectors({
            "setup": [{"ui": "填写", "选择器": "#a"}],
            "steps": [],
            "teardown": [{"ui": "点击", "选择器": "#b"}],
        })
        self.assertEqual(got, ["#a", "#b"])

    def test_collects_wait_conditions(self):
        got = ui.collect_selectors({
            "steps": [{"ui": "点击", "选择器": "#a",
                       "等待": {"选择器": ".loading", "状态": "hidden"}}],
        })
        self.assertEqual(got, ["#a", ".loading"])

    def test_wait_as_string(self):
        got = ui.collect_selectors({"steps": [{"ui": "等待", "选择器": "#a", "等待": ".ready"}]})
        self.assertEqual(got, ["#a", ".ready"])

    def test_deduplicates_and_keeps_order(self):
        got = ui.collect_selectors({
            "steps": [{"ui": "点击", "选择器": "#a"},
                      {"ui": "点击", "选择器": "#a"},
                      {"ui": "点击", "选择器": "#b"}],
        })
        self.assertEqual(got, ["#a", "#b"])

    def test_ignores_empty_and_non_dict(self):
        got = ui.collect_selectors({"steps": [None, "x", {"ui": "点击", "选择器": ""}]})
        self.assertEqual(got, [])


# ---------------------------------------------------------------- 浏览器选择

class TestResolveBrowser(unittest.TestCase):

    def test_explicit_executable_path_wins(self):
        kw, note = ui.resolve_browser({"executable_path": "/opt/chrome"})
        self.assertEqual(kw, {"executable_path": "/opt/chrome"})
        self.assertIn("/opt/chrome", note)

    def test_explicit_channel(self):
        kw, note = ui.resolve_browser({"channel": "chrome"})
        self.assertEqual(kw, {"channel": "chrome"})

    def test_explicit_bundled(self):
        kw, note = ui.resolve_browser({"bundled": True})
        self.assertEqual(kw, {})

    def test_prefers_system_chrome_when_present(self):
        # 系统装了 Chrome 就用它，省掉下载几百兆
        orig = ui._chrome_candidates
        ui._chrome_candidates = lambda: ["/fake/chrome"]
        orig_isfile = os.path.isfile
        os.path.isfile = lambda p: p == "/fake/chrome"
        try:
            kw, note = ui.resolve_browser()
        finally:
            ui._chrome_candidates = orig
            os.path.isfile = orig_isfile
        self.assertEqual(kw, {"channel": "chrome"})

    def test_falls_back_to_bundled(self):
        orig = ui._chrome_candidates
        ui._chrome_candidates = lambda: []
        orig_cached = ui._cached_chromium
        ui._cached_chromium = lambda: None
        try:
            kw, note = ui.resolve_browser()
        finally:
            ui._chrome_candidates = orig
            ui._cached_chromium = orig_cached
        self.assertEqual(kw, {})

    def test_availability_respects_kill_switch(self):
        old = os.environ.get("AUTOTEST_NO_UI")
        os.environ["AUTOTEST_NO_UI"] = "1"
        try:
            ok, reason = ui.is_available()
        finally:
            if old is None:
                os.environ.pop("AUTOTEST_NO_UI", None)
            else:
                os.environ["AUTOTEST_NO_UI"] = old
        self.assertFalse(ok)
        self.assertIn("AUTOTEST_NO_UI", reason)


# ---------------------------------------------------------------- 动作表

class TestUiActions(unittest.TestCase):

    def test_all_documented_actions_exist(self):
        # 动作表同时给 lint 与文档用，少一个会让规格通过 lint 但跑不了
        for name in ("点击", "填写", "选择", "按键", "等待", "悬停",
                     "滚动到", "切换frame", "断言快照", "识别验证码"):
            self.assertIn(name, ui.UI_ACTIONS)

    def test_unknown_action_raises(self):
        s = ui.UiSession()
        with self.assertRaises(UiError):
            s.act({"ui": "飞天"}, {})

    def test_missing_action_name_raises(self):
        s = ui.UiSession()
        with self.assertRaises(UiError):
            s.act({}, {})


class _Boom(object):
    """访问任何属性都抛异常 —— 模拟 Playwright 事件对象读取失败。"""

    def __getattr__(self, name):
        raise RuntimeError("读属性 %s 失败" % name)


class FakePage(object):
    """只记录注册了哪些回调，不启动浏览器。"""

    def __init__(self):
        self.handlers = {}

    def on(self, event, fn):
        self.handlers[event] = fn


class _BoomStr(object):
    """`str()` 会抛异常 —— `on_pageerror` 只做 str(err)，得用这个才测得出来。"""

    def __str__(self):
        raise RuntimeError("转字符串失败")


class _Msg(object):
    def __init__(self, type_="error", text="Uncaught TypeError"):
        self.type = type_
        self.text = text


class _Resp(object):
    def __init__(self, status=200, url="http://host/api/x", method="GET"):
        self.status = status
        self.url = url
        self.request = type("R", (), {"method": method})()


class _Req(object):
    def __init__(self, url="http://host/api/x", method="POST", failure="net::ERR_FAILED"):
        self.url = url
        self.method = method
        self.failure = failure


class EventHookTest(unittest.TestCase):
    """事件回调抛异常时必须留痕。

    这四个回调是「控制台」「网络」两类断言的唯一数据来源。回调静默失败
    等于证据缺失，而断言读的正是空列表 —— 会判通过，不会报错。这是本仓库
    最隐蔽的一类假通过，所以单独钉住。
    """

    def setUp(self):
        self.s = ui.UiSession()
        self.page = FakePage()
        self.s._page = self.page
        self.s._hook_events()

    def test_四个回调都挂上了(self):
        self.assertEqual(set(self.page.handlers),
                         {"console", "pageerror", "requestfailed", "response"})

    def test_console_error_正常记录(self):
        self.page.handlers["console"](_Msg("error", "Uncaught TypeError"))
        self.assertEqual(self.s.console_errors, ["Uncaught TypeError"])

    def test_console_warning_不记(self):
        self.page.handlers["console"](_Msg("warning", "[Vue warn]"))
        self.assertEqual(self.s.console_errors, [])

    def test_pageerror_正常记录(self):
        self.page.handlers["pageerror"](ValueError("x is not defined"))
        self.assertEqual(len(self.s.page_errors), 1)
        self.assertIn("x is not defined", self.s.page_errors[0])

    def test_response_4xx5xx_记入网络失败(self):
        self.page.handlers["response"](_Resp(500))
        self.assertEqual(self.s.network_failures[0]["status"], 500)

    def test_response_2xx_不记(self):
        self.page.handlers["response"](_Resp(200))
        self.assertEqual(self.s.network_failures, [])

    def test_requestfailed_正常记录(self):
        self.page.handlers["requestfailed"](_Req())
        self.assertIn("ERR_FAILED", self.s.network_failures[0]["failure"])

    def test_console_回调异常留痕且不产生假证据(self):
        self.page.handlers["console"](_Boom())
        self.assertEqual(len(self.s.diagnostics), 1)
        self.assertIn("控制台", self.s.diagnostics[0]["what"])
        self.assertIn("RuntimeError", self.s.diagnostics[0]["error"])
        self.assertEqual(self.s.console_errors, [])

    def test_pageerror_回调异常留痕(self):
        # str(err) 走的是类型上的 __str__，属性访问抛异常没用，必须让 __str__ 抛
        self.page.handlers["pageerror"](_BoomStr())
        self.assertIn("控制台", self.s.diagnostics[0]["what"])

    def test_response_回调异常留痕(self):
        self.page.handlers["response"](_Boom())
        self.assertIn("网络", self.s.diagnostics[0]["what"])

    def test_requestfailed_回调异常留痕(self):
        self.page.handlers["requestfailed"](_Boom())
        self.assertIn("网络", self.s.diagnostics[0]["what"])

    def test_回调异常不会冒泡到调用方(self):
        # Playwright 在事件分发里抛出的异常会打断后续回调，必须就地吃掉
        try:
            self.page.handlers["response"](_Boom())
        except Exception as e:                       # pragma: no cover
            self.fail("回调异常不该冒泡：%r" % e)

    def test_同样的异常只记一次(self):
        self.page.handlers["response"](_Boom())
        self.page.handlers["response"](_Boom())
        self.assertEqual(len(self.s.diagnostics), 1)


# ---------------------------------------------------------------- 请求证据等待

class NetSession(object):
    """只有「请求级网络断言」相关接口的假会话。"""

    def __init__(self, requests=None, seen=0):
        self.network_requests = requests or []
        self.network_seen = seen
        self.diagnostics = []
        self.idle_calls = []

    def _diag(self, what, exc=None):
        self.diagnostics.append({"what": what})

    def wait_network_idle(self, timeout_ms=None):
        self.idle_calls.append(timeout_ms)


def nreq(seq, url="http://h/api/order/submit", method="POST"):
    return {"seq": seq, "url": url, "method": method, "status": 200, "body": ""}


class TestRequestEvidencePatterns(unittest.TestCase):

    def test_取请求与方法(self):
        st = {"断言": [{"类型": "网络", "请求": "/api/a", "方法": "post", "次数": 1}]}
        self.assertEqual(ui.request_evidence_patterns(st), [("POST", "/api/a")])

    def test_次数为0不等待它出现(self):
        st = {"断言": [{"类型": "网络", "请求": "/api/a", "次数": 0}]}
        self.assertEqual(ui.request_evidence_patterns(st), [])

    def test_次数字典等于0也算反向断言(self):
        st = {"断言": [{"类型": "网络", "请求": "/api/a", "次数": {"等于": 0}}]}
        self.assertEqual(ui.request_evidence_patterns(st), [])

    def test_次数字典不等0仍要等(self):
        st = {"断言": [{"类型": "网络", "请求": "/api/a", "次数": {"大于等于": 1}}]}
        self.assertEqual(len(ui.request_evidence_patterns(st)), 1)

    def test_无失败请求不触发等待(self):
        st = {"断言": [{"类型": "网络", "无失败请求": True}]}
        self.assertEqual(ui.request_evidence_patterns(st), [])
        self.assertFalse(ui.has_request_assertion(st))

    def test_有请求断言才触发等待(self):
        st = {"断言": [{"类型": "网络", "请求": "/api/a", "次数": 1}]}
        self.assertTrue(ui.has_request_assertion(st))


class TestAwaitRequestEvidence(unittest.TestCase):

    def test_请求已经出现立刻返回(self):
        s = NetSession([nreq(3)])
        st = {"断言": [{"类型": "网络", "请求": "/api/order", "次数": 1}]}
        self.assertTrue(ui.await_request_evidence(s, st, {}, timeout_ms=200))
        self.assertEqual(s.diagnostics, [])

    def test_只认本步骤之后的请求(self):
        # 这是实测踩到的坑：上一个用例调过同一接口，扫描全会话会立刻"看见"旧请求，
        # 等待白返回，本步仍然数到 0 次。
        s = NetSession([nreq(1)])
        st = {"断言": [{"类型": "网络", "请求": "/api/order", "次数": 1}]}
        ok = ui.await_request_evidence(s, st, {}, timeout_ms=150, since=5)
        self.assertFalse(ok)
        self.assertTrue(any("没发出来" in d["what"] for d in s.diagnostics))

    def test_超时记诊断并返回False(self):
        s = NetSession([])
        st = {"断言": [{"类型": "网络", "请求": "/api/never", "次数": 1}]}
        self.assertFalse(ui.await_request_evidence(s, st, {}, timeout_ms=120))
        self.assertIn("/api/never", s.diagnostics[0]["what"])

    def test_请求片段支持变量插值(self):
        s = NetSession([nreq(1, url="http://h/api/order/42")])
        st = {"断言": [{"类型": "网络", "请求": "/api/order/${id}", "次数": 1}]}
        self.assertTrue(ui.await_request_evidence(s, st, {"id": 42}, timeout_ms=200))

    def test_方法不匹配不算证据(self):
        s = NetSession([nreq(2, method="GET")])
        st = {"断言": [{"类型": "网络", "请求": "/api/order", "方法": "POST", "次数": 1}]}
        self.assertFalse(ui.await_request_evidence(s, st, {}, timeout_ms=120))


class TestPrepareRequestEvidence(unittest.TestCase):

    def _step(self, assertion, idle_ms=120):
        return {"断言": [assertion], "网络空闲超时": idle_ms}

    def test_有请求断言时等证据再等安静(self):
        s = NetSession([nreq(1)])
        ui.prepare_request_evidence(s, self._step(
            {"类型": "网络", "请求": "/api/order", "次数": 1}), {})
        self.assertEqual(s.idle_calls, [120])
        self.assertEqual(s.diagnostics, [])

    def test_次数0也要等安静(self):
        # 反向断言同样怕"晚发的请求还没到就判了没调过"
        s = NetSession([])
        ui.prepare_request_evidence(s, self._step(
            {"类型": "网络", "请求": "/api/order", "次数": 0}), {})
        self.assertEqual(s.idle_calls, [120])

    def test_没有请求断言时完全不动(self):
        s = NetSession([])
        ui.prepare_request_evidence(s, self._step(
            {"类型": "网络", "无失败请求": True}), {})
        self.assertEqual(s.idle_calls, [])

    def test_等待中出错只记诊断不抛出(self):
        class Boom(NetSession):
            def wait_network_idle(self, timeout_ms=None):
                raise RuntimeError("driver 挂了")

        s = Boom([nreq(1)])
        ui.prepare_request_evidence(s, self._step(
            {"类型": "网络", "请求": "/api/order", "次数": 1}), {})
        self.assertTrue(any("等待出错" in d["what"] for d in s.diagnostics))


# ---------------------------------------------------------------- 上传 / 下载

class TestArtifactHelpers(unittest.TestCase):
    """下载文件名来自服务端，必须当成不可信输入处理。"""

    def test_剥掉路径只留文件名(self):
        self.assertEqual(ui._safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(ui._safe_filename("C:\\temp\\a.xlsx"), "a.xlsx")

    def test_替换非法字符(self):
        self.assertEqual(ui._safe_filename('a:b*c?.txt'), "a_b_c_.txt")
        self.assertEqual(ui._safe_filename("a\nb.txt"), "a_b.txt")

    def test_空名字给兜底(self):
        self.assertEqual(ui._safe_filename(""), "download.bin")
        self.assertEqual(ui._safe_filename(None), "download.bin")

    def test_中文名原样保留(self):
        self.assertEqual(ui._safe_filename("月度报表.xlsx"), "月度报表.xlsx")

    def test_摘要按文件内容算(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.bin")
        with open(p, "wb") as f:
            f.write(b"abc")
        # sha256("abc")
        self.assertEqual(
            ui._sha256(p),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class TestArtifactWindow(unittest.TestCase):
    """下载产物必须按步骤窗口切，不能跨用例累积。"""

    def test_窗口从动作前打点开始(self):
        s = FakeSession()
        s.downloads = [{"name": "上个用例.xlsx"}]      # 早就存在的产物
        s.uploads = [{"files": ["/tmp/old.pdf"]}]
        ui.run_ui_step(s, {"ui": "断言快照",
                           "断言": [{"类型": "下载", "后缀": ".xlsx"}]},
                       "http://host", {})
        _net, dl_since, up_since = s.last_windows
        self.assertEqual(dl_since, 1)
        self.assertEqual(up_since, 1)

    def test_空会话窗口是0(self):
        s = FakeSession()
        ui.run_ui_step(s, {"ui": "断言快照",
                           "断言": [{"类型": "页面", "选择器": "h1", "可见": True}]},
                       "http://host", {})
        _net, dl_since, up_since = s.last_windows
        self.assertEqual((dl_since, up_since), (0, 0))

    def test_下载断言读到的是本步骤窗口内容(self):
        # 快照里带回的 downloads 由真实 session 按窗口切片，这里模拟它只给本步产物
        s = FakeSession(snapshot={
            "url": "http://host/home", "text": "", "elements": {},
            "console_errors": [], "page_errors": [], "network_failures": [],
            "downloads": [{"name": "本次.csv", "path": "", "size": 3,
                           "sha256": "abc", "error": ""}],
            "diagnostics": [],
        })
        resp, _ = run({"ui": "断言快照",
                       "断言": [{"类型": "下载", "后缀": ".csv"}]}, s)
        self.assertEqual([d["name"] for d in resp["downloads"]], ["本次.csv"])


if __name__ == "__main__":
    unittest.main()
