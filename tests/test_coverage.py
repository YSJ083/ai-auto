"""需求覆盖的单元测试 —— 不联网、不起浏览器。

为什么这块必须单独钉：
    覆盖率算错不会崩，只会**变好看**。少算一条需求、把 manual 当成通过、
    把 --only 的部分执行当成全量 —— 每一种都让报告显示「覆盖 100%」，
    而实际上有需求根本没测。这种错误没有任何报错来提醒你，
    只能靠在测试里把每一种「看起来达标其实没有」的情形逐个钉死。

这里钉五类：
  1. 状态聚合取最差（一条需求下有一条失败，整条就是失败）
  2. manual / skipped 不算已验证（人工和未执行都不是「测过了」）
  3. 未覆盖与失败分开（一个是用例都没写，一个是用例没过）
  4. 分母来源（声明 vs 观测）必须标出来，否则 100% 的含义会被误读
  5. 筛选执行 / 提前中断一律标 partial（范围内的覆盖度不是覆盖度）
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import compile as C        # noqa: E402
from core import coverage as CV      # noqa: E402
from core import report as RP        # noqa: E402


def row(cid, status="passed", reqs=None):
    return {"id": cid, "module": "登录", "title": "t", "priority": "P1", "status": status,
            "flaky": False, "duration_ms": 1, "reason": "", "basis": "",
            "steps": [], "assertions": {"passed": 1, "total": 1},
            "requirements": list(reqs or [])}


class NormalizeTest(unittest.TestCase):

    def test_字符串数组(self):
        self.assertEqual([{"id": "R-1", "说明": ""}, {"id": "R-2", "说明": ""}],
                         CV.normalize_requirements({"需求": ["R-1", "R-2"]}))

    def test_对象数组带说明(self):
        got = CV.normalize_requirements({"需求": [{"id": "R-1", "说明": "登录"}]})
        self.assertEqual([{"id": "R-1", "说明": "登录"}], got)

    def test_混排与去重(self):
        got = CV.normalize_requirements({"需求": ["R-1", {"id": "R-2"}, "R-1"]})
        self.assertEqual(["R-1", "R-2"], [x["id"] for x in got])

    def test_没有需求字段就是空(self):
        self.assertEqual([], CV.normalize_requirements({}))
        self.assertEqual([], CV.normalize_requirements(None))

    def test_空ID被丢掉(self):
        # 从表格里粘过来常带空行，不该变成一条叫 "" 的需求
        self.assertEqual([], CV.normalize_requirements({"需求": ["", "  "]}))
        self.assertEqual(["R-1"], [x["id"] for x in
                                  CV.normalize_requirements({"需求": ["", "R-1"]})])


class CaseReqTest(unittest.TestCase):

    def test_单个值也认(self):
        self.assertEqual(["R-1"], CV.case_requirements({"需求点": "R-1"}))

    def test_数组与去重(self):
        self.assertEqual(["R-1", "R-2"],
                         CV.case_requirements({"需求点": ["R-1", "R-2", "R-1"]}))

    def test_没标就是空(self):
        self.assertEqual([], CV.case_requirements({"id": "TC-1"}))
        self.assertEqual([], CV.case_requirements(None))


class ShapeTest(unittest.TestCase):

    def test_合法形状没有意见(self):
        self.assertEqual([], CV.validate_requirements_shape(None, "x"))
        self.assertEqual([], CV.validate_requirements_shape("R-1", "x"))
        self.assertEqual([], CV.validate_requirements_shape(["R-1"], "x"))

    def test_空字符串报错(self):
        self.assertTrue(CV.validate_requirements_shape("", "x"))

    def test_空数组只是提示(self):
        got = CV.validate_requirements_shape([], "x")
        self.assertEqual("warning", got[0][0])

    def test_列表里有非字符串报错(self):
        got = CV.validate_requirements_shape(["R-1", 2], "x")
        self.assertEqual("error", got[0][0])

    def test_类型不对报错(self):
        self.assertTrue(CV.validate_requirements_shape({"a": 1}, "x"))

    def test_引用未声明ID报错(self):
        got = CV.check_against_declared({"需求点": ["R-9"]}, {"R-1"}, "TC-1")
        self.assertEqual(1, len(got))
        self.assertIn("R-9", got[0][1])

    def test_没声明需求清单时不检查(self):
        self.assertEqual([], CV.check_against_declared({"需求点": ["R-9"]}, set(), "TC-1"))


class BuildTest(unittest.TestCase):

    def build(self, spec, results, **kw):
        return CV.build_coverage(spec, results, **kw)

    def test_声明三个只有一个有用例(self):
        cov = self.build({"需求": ["R-1", "R-2"]},
                         [row("TC-1", reqs=["R-1"])])
        self.assertEqual(2, cov["total"])
        self.assertEqual(1, cov["covered"])
        self.assertEqual(1, cov["verified"])
        self.assertEqual(["R-2"], cov["uncovered"])
        self.assertEqual("spec", cov["declared_source"])

    def test_一条需求下有失败整条算失败(self):
        # 2 过 1 挂。按「多数通过」算会让漏测看起来像达标
        cov = self.build({}, [row("TC-1", reqs=["R-1"]),
                              row("TC-2", reqs=["R-1"]),
                              row("TC-3", "failed", reqs=["R-1"])])
        self.assertEqual(["R-1"], cov["failed"])
        self.assertEqual(0, cov["verified"])
        self.assertEqual("failed", cov["items"][0]["status"])

    def test_失败优先于阻断(self):
        cov = self.build({}, [row("TC-1", "blocked", reqs=["R-1"]),
                              row("TC-2", "failed", reqs=["R-1"])])
        self.assertEqual("failed", cov["items"][0]["status"])

    def test_只有人工用例不算已验证(self):
        cov = self.build({}, [row("TC-1", "manual", reqs=["R-1"])])
        self.assertEqual(["R-1"], cov["manual"])
        self.assertEqual(0, cov["verified"])
        self.assertEqual(1, cov["covered"])

    def test_跳过不算已验证(self):
        cov = self.build({}, [row("TC-1", "skipped", reqs=["R-1"])])
        self.assertEqual(["R-1"], cov["skipped"])
        self.assertEqual(0, cov["verified"])

    def test_未覆盖与失败分开统计(self):
        cov = self.build({"需求": ["R-1", "R-2"]},
                         [row("TC-1", "failed", reqs=["R-1"])])
        self.assertEqual(["R-2"], cov["uncovered"])
        self.assertEqual(["R-1"], cov["failed"])
        self.assertEqual(1, cov["covered"])

    def test_没声明需求时按观测算并标明来源(self):
        cov = self.build({}, [row("TC-1", reqs=["R-1", "R-2"])])
        self.assertEqual("observed", cov["declared_source"])
        self.assertEqual(2, cov["total"])
        self.assertEqual(100.0, cov["rate"])

    def test_完全没标需求点就整块关闭(self):
        # 老规格不该因为升级多出一堆空表
        cov = self.build({}, [row("TC-1")])
        self.assertFalse(cov["enabled"])
        self.assertEqual(["TC-1"], cov["untracked_cases"])

    def test_没标需求点的用例不影响分母(self):
        cov = self.build({"需求": ["R-1"]},
                         [row("TC-1", reqs=["R-1"]), row("TC-2")])
        self.assertEqual(1, cov["total"])
        self.assertEqual(["TC-2"], cov["untracked_cases"])
        self.assertEqual(1, cov["tracked_cases"])

    def test_用例引用了未声明的ID也计入(self):
        # lint 会拦，但手改 result 或老数据不该让报告崩
        cov = self.build({"需求": ["R-1"]}, [row("TC-1", reqs=["R-1", "R-9"])])
        self.assertEqual(2, cov["total"])
        self.assertFalse(cov["items"][1]["declared"])

    def test_空需求清单等于没声明(self):
        cov = self.build({"需求": []}, [row("TC-1", reqs=["R-1"])])
        self.assertEqual("observed", cov["declared_source"])

    def test_筛选执行标为partial(self):
        cov = self.build({}, [row("TC-1", reqs=["R-1"])], selected="TC-1")
        self.assertTrue(cov["partial_run"])
        self.assertEqual("TC-1", cov["filter"])

    def test_提前中断标为partial(self):
        cov = self.build({}, [row("TC-1", reqs=["R-1"])], interrupted=True)
        self.assertTrue(cov["partial_run"])

    def test_全量执行不标partial(self):
        cov = self.build({}, [row("TC-1", reqs=["R-1"])])
        self.assertFalse(cov["partial_run"])

    def test_费率只算已验证(self):
        cov = self.build({"需求": ["R-1", "R-2", "R-3"]},
                         [row("TC-1", reqs=["R-1"]),
                          row("TC-2", "failed", reqs=["R-2"])])
        self.assertEqual(33.3, cov["rate"])

    def test_一条用例覆盖多个需求(self):
        cov = self.build({}, [row("TC-1", reqs=["R-1", "R-2"])])
        self.assertEqual(2, len(cov["items"]))
        self.assertEqual(["TC-1"], cov["items"][0]["cases"])

    def test_声明顺序稳定(self):
        cov = self.build({"需求": ["R-2", "R-1"]},
                         [row("TC-1", reqs=["R-1", "R-2"])])
        self.assertEqual(["R-2", "R-1"], [i["id"] for i in cov["items"]])


class CompileTest(unittest.TestCase):
    """编译期就要拦住「拼错的需求 ID」，否则覆盖度会静默变低。"""

    def payload(self, execu_extra=None, meta=None):
        e = {"步骤": [{"名称": "查", "method": "GET", "path": "/x",
                       "断言": [{"类型": "状态码", "等于": 200}]}]}
        e.update(execu_extra or {})
        return {"meta": meta or {}, "cases": [
            {"id": "TC-1", "用例标题": "查询", "所属模块": "登录", "预期结果": "返回 200",
             "测试步骤": "GET /x", "执行": e}]}

    def test_需求点带进规格(self):
        spec, problems = C.compile_cases(self.payload({"需求点": ["R-1", "R-2"]}, {"需求": ["R-1", "R-2"]}))
        self.assertEqual([], [m for lv, m in problems if lv == "error"])
        self.assertEqual(["R-1", "R-2"], spec["cases"][0]["需求点"])
        self.assertEqual(["R-1", "R-2"], [d["id"] for d in spec["需求"]])

    def test_没写需求点就不产生这个键(self):
        spec, _ = C.compile_cases(self.payload())
        self.assertEqual([], spec["cases"][0]["需求点"])
        self.assertNotIn("需求", spec)

    def test_引用未声明ID报错(self):
        _, problems = C.compile_cases(self.payload({"需求点": ["R-9"]}, {"需求": ["R-1"]}))
        self.assertTrue(any("R-9" in m and lv == "error" for lv, m in problems))

    def test_需求点形状不对报错(self):
        _, problems = C.compile_cases(self.payload({"需求点": 3}))
        self.assertTrue(any("需求点" in m and lv == "error" for lv, m in problems))

    def test_人工用例也要记需求点(self):
        p = self.payload()
        p["cases"][0]["执行"] = {"自动化": "manual", "需求点": ["R-1"]}
        spec, _ = C.compile_cases(p)
        self.assertEqual(["R-1"], spec["cases"][0]["需求点"])

    def test_lint拦下未声明的需求点(self):
        spec = {"base_url": "http://h", "需求": ["R-1"], "cases": [
            {"id": "TC-1", "需求点": ["R-2"], "steps": [
                {"method": "GET", "path": "/x", "断言": [{"类型": "状态码", "等于": 200}]}]}]}
        problems = C.lint_spec(spec)
        self.assertTrue(any("R-2" in m for lv, m in problems))

    def test_lint拦下需求字段不是数组(self):
        spec = {"base_url": "http://h", "需求": "R-1", "cases": [
            {"id": "TC-1", "steps": []}]}
        problems = C.lint_spec(spec)
        self.assertTrue(any("需求" in m and lv == "error" for lv, m in problems))

    def test_声明需求但一条都没标只提示不报错(self):
        # 报错会逼着人删掉需求清单，那就等于放弃覆盖度；提示更合适
        spec = {"base_url": "http://h", "需求": ["R-1", "R-2"], "cases": [
            {"id": "TC-1", "steps": [
                {"method": "GET", "path": "/x", "断言": [{"类型": "状态码", "等于": 200}]}]}]}
        problems = C.lint_spec(spec)
        self.assertTrue(any("没有任何用例标" in m and lv == "warning" for lv, m in problems))
        self.assertEqual([], [m for lv, m in problems if lv == "error"])


def _result(cases, coverage=None):
    def n(status):
        return len([c for c in cases if c["status"] == status])
    return {
        "schema": "autotest.result/2",
        "tool": {"name": "ai-auto-test", "version": "1.8.0"},
        "run": {"id": "abc", "spec_name": "覆盖度", "spec_version": "1.0",
                "started_at": "2026-10-22 16:00:00", "finished_at": "2026-10-22 16:00:01",
                "duration_ms": 1000, "spec_path": "/tmp/x.json", "filter": ""},
        "target": {"env": "test", "base": "http://h", "auth_type": "none",
                   "credential_ok": True, "credential_error": "",
                   "credential_source": "", "ui": {"enabled": False}},
        "summary": {"total": len(cases), "auto": len(cases), "passed": n("passed"),
                    "failed": n("failed"), "blocked": n("blocked"),
                    "skipped": n("skipped"), "manual": n("manual"), "flaky": 0,
                    "pass_rate": 100.0, "assertions_total": len(cases),
                    "assertions_passed": len(cases), "data_rows": 0},
        "coverage": coverage,
        "cases": cases, "defects": [],
    }


class ReportTest(unittest.TestCase):
    """报告要说出「没覆盖」，否则全绿就是假安全。"""

    def cov(self, spec, rows, **kw):
        return CV.build_coverage(spec, rows, **kw)

    def test_结论里点明未覆盖(self):
        c = self.cov({"需求": ["R-1", "R-2"]}, [row("TC-1", reqs=["R-1"])])
        md = RP.render_md(_result([row("TC-1", reqs=["R-1"])], c))
        self.assertIn("需求覆盖 **1/2**", md)
        self.assertIn("R-2", md)
        self.assertIn("不代表这些需求被测过", md)

    def test_有独立的覆盖章节和明细表(self):
        c = self.cov({"需求": ["R-1"]}, [row("TC-1", reqs=["R-1"])])
        md = RP.render_md(_result([row("TC-1", reqs=["R-1"])], c))
        self.assertIn("## 四、需求覆盖", md)
        self.assertIn("| 需求点 | 说明 | 状态 | 关联用例 |", md)
        # 用例明细给覆盖章节让位，序号顺延
        self.assertIn("## 五、用例明细", md)
        self.assertIn("已验证", md)

    def test_未覆盖状态有独立措辞(self):
        c = self.cov({"需求": ["R-1"]}, [])
        md = RP.render_md(_result([], c))
        self.assertIn("未覆盖", md)

    def test_观测口径要写明分母来源(self):
        c = self.cov({}, [row("TC-1", reqs=["R-1"])])
        md = RP.render_md(_result([row("TC-1", reqs=["R-1"])], c))
        self.assertIn("不说明需求清单", md)

    def test_筛选执行要写警告(self):
        c = self.cov({}, [row("TC-1", reqs=["R-1"])], selected="TC-1")
        md = RP.render_md(_result([row("TC-1", reqs=["R-1"])], c))
        self.assertIn("只代表所选范围", md)

    def test_老结果没有覆盖字段时报告不变(self):
        md = RP.render_md(_result([row("TC-1", reqs=["R-1"])]))
        self.assertNotIn("需求覆盖", md)
        self.assertIn("## 四、用例明细", md)

    def test_HTML有卡片与未覆盖状态(self):
        c = self.cov({"需求": ["R-1", "R-2"]}, [row("TC-1", reqs=["R-1"])])
        h = RP.render_html(_result([row("TC-1", reqs=["R-1"])], c))
        self.assertIn("需求覆盖", h)
        self.assertIn('class="v">1/2', h)
        self.assertIn("c-uncovered", h)

    def test_HTML没覆盖字段时不出卡片(self):
        h = RP.render_html(_result([row("TC-1", reqs=["R-1"])]))
        self.assertNotIn("需求覆盖", h)


class SummarizeTest(unittest.TestCase):

    def test_一句话带未覆盖数(self):
        c = CV.build_coverage({"需求": ["R-1", "R-2"]}, [row("TC-1", reqs=["R-1"])])
        line = CV.summarize(c)
        self.assertIn("1/2", line)      # 2 条需求里只有 1 条有用例
        self.assertIn("1 个需求点无用例", line)

    def test_没启用时不出声(self):
        self.assertEqual("", CV.summarize({"enabled": False}))
        self.assertEqual("", CV.summarize(None))


if __name__ == "__main__":
    unittest.main()
