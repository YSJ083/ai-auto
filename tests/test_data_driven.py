"""数据驱动（用例层 `数据` 数组）的单元测试 —— 不联网、不起浏览器。

为什么这块值得单独钉：
    展开逻辑错了不会崩，只会安静地跑错 —— 少展开一行就是少测一组边界值，
    变量没覆盖就是所有行跑同一份数据、报告里却显示「跑了 5 组」。
    这比报错危险得多，所以每一处取舍都钉一条：
      - id 后缀、_data 元信息（报告要靠它区分行）
      - 数据组的键覆盖用例级 vars，且不就地改原用例
      - 依赖基础 id 时要求每一行都通过
      - 编译期把形状错误拦在跑之前
      - 请求体里的数字/布尔不被转成字符串
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import compile as C      # noqa: E402
from core import report as RP     # noqa: E402
from core import runner as R      # noqa: E402
from core import asserts as A     # noqa: E402
from core.asserts import fill     # noqa: E402


def case(cid="TC-1", **kw):
    c = {"id": cid, "模块": "设备", "用例标题": "按参数查询",
         "自动化": "auto",
         "steps": [{"名称": "查", "method": "GET", "path": "/x",
                    "断言": [{"类型": "状态码", "等于": 200}]}]}
    c.update(kw)
    return c


class ExpandTest(unittest.TestCase):
    """展开：一条用例摊成多行。"""

    def test_没写数据就不展开(self):
        c = case()
        out = R._expand_cases([c])
        self.assertEqual(1, len(out))
        self.assertIs(c, out[0])

    def test_三组数据摊成三行且id带序号(self):
        out = R._expand_cases([case(数据=[{"u": "a"}, {"u": "b"}, {"u": "c"}])])
        self.assertEqual(["TC-1#1", "TC-1#2", "TC-1#3"], [x["id"] for x in out])
        self.assertEqual([1, 2, 3], [x["_data"]["index"] for x in out])
        self.assertEqual(3, out[0]["_data"]["total"])
        self.assertEqual("TC-1", out[0]["_data"]["base_id"])

    def test_标题不动(self):
        # 标题是报告分组和缺陷归并的依据，改标题会让同一条用例没法归并
        out = R._expand_cases([case(数据=[{"u": "a"}, {"u": "b"}])])
        self.assertEqual({"按参数查询"}, {x["用例标题"] for x in out})

    def test_数据组的键覆盖用例级vars(self):
        c = case(vars={"u": "公共", "keep": 1}, 数据=[{"u": "行内"}])
        out = R._expand_cases([c])
        self.assertEqual("行内", out[0]["vars"]["u"])
        self.assertEqual(1, out[0]["vars"]["keep"])
        # 原用例不能被就地改掉：同一个 spec 可能被跑第二遍
        self.assertEqual("公共", c["vars"]["u"])

    def test_展开后不留数据字段(self):
        out = R._expand_cases([case(数据=[{"a": 1}])])
        self.assertNotIn("数据", out[0])

    def test_数据形状不对直接报错(self):
        for bad in ([1, 2], {"a": 1}, "x"):
            with self.assertRaises(ValueError):
                R._expand_cases([case(数据=bad)])

    def test_没数据的用例保持原顺序(self):
        out = R._expand_cases([case("TC-1"), case("TC-2", 数据=[{"a": 1}]),
                               case("TC-3")])
        self.assertEqual(["TC-1", "TC-2#1", "TC-3"], [x["id"] for x in out])


class DependencyTest(unittest.TestCase):
    """依赖：写基础 id 时要看整组，写 #N 时只看那一行。"""

    def test_有一行没过就算依赖不满足(self):
        by = {"TC-1#1": "passed", "TC-1#2": "failed"}
        self.assertEqual(["TC-1"], R._bad_deps(["TC-1"], by))

    def test_每一行都过才算满足(self):
        by = {"TC-1#1": "passed", "TC-1#2": "passed"}
        self.assertEqual([], R._bad_deps(["TC-1"], by))

    def test_可以点名某一行(self):
        by = {"TC-1#1": "passed", "TC-1#2": "failed"}
        self.assertEqual([], R._bad_deps(["TC-1#1"], by))
        self.assertEqual(["TC-1#2"], R._bad_deps(["TC-1#2"], by))

    def test_依赖不存在的用例仍然算坏(self):
        self.assertEqual(["TC-9"], R._bad_deps(["TC-9"], {}))
        # 前缀碰巧相同的别的用例不能被误认成展开行
        self.assertEqual(["TC-1"], R._bad_deps(["TC-1"], {"TC-10#1": "passed"}))


class LintTest(unittest.TestCase):
    """编译期：形状错、变量不可溯源、依赖越界，都要在跑之前拦下。"""

    def lint(self, *cases):
        spec = {"base_url": "http://h", "cases": list(cases)}
        return [m for lv, m in C.lint_spec(spec) if lv == "error"]

    def test_合法的数据驱动没有错误(self):
        self.assertEqual([], self.lint(case(数据=[{"u": "a", "n": 1}])))

    def test_空数组被拦下(self):
        self.assertTrue(any("非空数组" in m for m in self.lint(case(数据=[]))))

    def test_不是数组被拦下(self):
        self.assertTrue(any("非空数组" in m for m in self.lint(case(数据={"u": "a"}))))

    def test_行是标量被拦下(self):
        self.assertTrue(any("必须是对象" in m for m in self.lint(case(数据=[1, 2]))))

    def test_数据里的键算已定义变量(self):
        steps = [{"名称": "查", "method": "GET", "path": "/x",
                  "断言": [{"类型": "JSON字段", "路径": "data.name", "等于": "${账号}"}]}]
        # 有数据组：${账号} 有来源，不该报未定义
        self.assertEqual([], self.lint(case(数据=[{"账号": "zrgj"}], steps=steps)))
        # 没数据组也没提取过：必须报
        self.assertTrue(any("账号" in m for m in self.lint(case(steps=steps))))

    def test_依赖越界的数据组被拦下(self):
        self.assertTrue(any("只有 2 组数据" in m
                            for m in self.lint(case("TC-1", 数据=[{"a": 1}, {"a": 2}]),
                                               case("TC-2", 依赖=["TC-1#3"]))))

    def test_依赖非数据驱动用例的行号被拦下(self):
        self.assertTrue(any("不是数据驱动" in m
                            for m in self.lint(case("TC-1"), case("TC-2", 依赖=["TC-1#2"]))))

    def test_依赖基础id合法(self):
        self.assertEqual([], self.lint(case("TC-1", 数据=[{"a": 1}]),
                                       case("TC-2", 依赖=["TC-1"])))


class FillTypeTest(unittest.TestCase):
    """请求体里的值要保留类型，否则 18 会变成 "18"。"""

    def test_整串引用保留数字类型(self):
        v = fill({"age": "${age}"}, {"age": 18}, keep_types=True)
        self.assertEqual({"age": 18}, v)
        self.assertIsInstance(v["age"], int)

    def test_整串引用保留布尔和对象(self):
        v = fill({"ok": "${ok}", "obj": "${obj}"},
                 {"ok": True, "obj": {"a": 1}}, keep_types=True)
        self.assertIs(True, v["ok"])
        self.assertEqual({"a": 1}, v["obj"])

    def test_拼接场景仍然转字符串(self):
        v = fill({"name": "user-${n}"}, {"n": 7}, keep_types=True)
        self.assertEqual({"name": "user-7"}, v)

    def test_默认行为没变(self):
        # 不开 keep_types 时维持老行为，避免影响 URL / 头部等既有拼接
        self.assertEqual({"n": "7"}, fill({"n": "${n}"}, {"n": 7}))

    def test_未定义变量原样保留(self):
        self.assertEqual({"n": "${nope}"}, fill({"n": "${nope}"}, {}, keep_types=True))


def _result(cases, data_rows=0):
    def n(status):
        return len([c for c in cases if c["status"] == status])
    return {
        "schema": "autotest.result/2",
        "tool": {"name": "ai-auto-test", "version": "1.7.0"},
        "run": {"id": "abc", "spec_name": "数据驱动", "spec_version": "1.0",
                "started_at": "2026-10-22 15:00:00", "finished_at": "2026-10-22 15:00:01",
                "duration_ms": 1000, "spec_path": "/tmp/x.json", "filter": ""},
        "target": {"env": "test", "base": "http://h", "auth_type": "none",
                   "credential_ok": True, "credential_error": "",
                   "credential_source": "", "ui": {"enabled": False}},
        "summary": {"total": len(cases), "auto": len(cases), "passed": n("passed"),
                    "failed": n("failed"), "blocked": n("blocked"),
                    "skipped": n("skipped"), "manual": n("manual"),
                    "flaky": 0, "pass_rate": 100.0,
                    "assertions_total": len(cases), "assertions_passed": len(cases),
                    "data_rows": data_rows},
        "cases": cases, "defects": [],
    }


def _row(cid, title="按参数查询", data=None, status="passed"):
    return {"id": cid, "module": "设备", "title": title, "priority": "P1",
            "type": "接口", "status": status, "flaky": False, "duration_ms": 10,
            "reason": "", "basis": "", "defect_level": "",
            "steps": [], "assertions": {"passed": 1, "total": 1}, "data": data}


class ReportTest(unittest.TestCase):
    """报告要能说清「为什么同一个标题出现多次」。"""

    def test_有数据时总览多一行展开说明(self):
        rows = [_row("TC-1#1", data={"index": 1, "total": 2,
                                     "values": {"u": "a"}, "base_id": "TC-1"}),
                _row("TC-1#2", data={"index": 2, "total": 2,
                                     "values": {"u": "b"}, "base_id": "TC-1"})]
        md = RP.render_md(_result(rows, data_rows=2))
        self.assertIn("数据驱动展开", md)
        self.assertIn("2 行（来自 1 条用例）", md)

    def test_没有数据时不出现这一节(self):
        md = RP.render_md(_result([_row("TC-1")]))
        self.assertNotIn("数据驱动展开", md)

    def test_未执行完的用例也写明是第几组数据(self):
        rows = [_row("TC-1#2", data={"index": 2, "total": 3,
                                     "values": {"u": "b"}, "base_id": "TC-1"},
                     status="blocked")]
        md = RP.render_md(_result(rows, data_rows=1))
        self.assertIn("数据组 2/3", md)
        self.assertIn("u=b", md)


class AssertVarTypeTest(unittest.TestCase):
    """断言侧的 `${变量}` 也要保留类型 —— 这是端到端跑出来才发现的坑。

    数据组里写 `金额: 0`，若断言侧把 `${金额}` 转成 "0"，而响应里的 0 是数字，
    比较永远不等；表现是一条必然失败，且失败信息写着「amount=0（期望 0）」，
    看上去像产品缺陷，实际是工具自己丢的类型。
    """

    def judge(self, assertion, body, variables=None):
        resp = {"status": 200, "json": body, "text": "", "headers": {},
                "elapsed_ms": 1, "ui": False, "url": ""}
        return A.judge(assertion, resp, variables or {})[0]

    def test_数字变量的等于断言能通过(self):
        self.assertTrue(self.judge({"类型": "JSON字段", "路径": "amount", "等于": "${金额}"},
                                   {"amount": 0}, {"金额": 0}))

    def test_布尔变量的等于断言能通过(self):
        self.assertTrue(self.judge({"类型": "JSON字段", "路径": "ok", "等于": "${ok}"},
                                   {"ok": True}, {"ok": True}))

    def test_成功标志用布尔变量能通过(self):
        # 成功标志用的是 is 比较，转成 "true" 必假
        self.assertTrue(self.judge({"类型": "成功标志", "等于": "${ok}"},
                                   {"success": True}, {"ok": True}))

    def test_字符串变量仍按字符串比(self):
        self.assertTrue(self.judge({"类型": "JSON字段", "路径": "name", "等于": "${n}"},
                                   {"name": "zrgj"}, {"n": "zrgj"}))

    def test_数字期望对数字字符串是宽松匹配_既有语义(self):
        """钉住既有行为：`等于 0` 认为 `"0"` 成立（_num_eq 会转数字比）。

        这是刻意的，不是漏判：不少后端把 BigDecimal 序列化成字符串，
        严格比类型会把正常系统报成缺陷。但也意味着「服务端把 0 返回成 "0"」
        这类类型问题它看不出来 —— 需要更严就写 `正则` 或加一条 `是字符串` 断言。
        """
        self.assertTrue(self.judge({"类型": "JSON字段", "路径": "amount", "等于": 0},
                                   {"amount": "0"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
