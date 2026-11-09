"""report.py 渲染的单元测试。

报告是这套工具唯一对外交付物，格式化字符串写错不会报错，只会让某一节
凭空消失或显示成 `HTTP None`。这里钉住三件事：

1. 有采集诊断时出现「采集诊断」一节，没有时整节不出现（不留空壳）
2. UI 步骤没有导航时显示「无导航」，不显示 `HTTP None`
3. 同一诊断现象在多个用例里出现，只归并成一条

运行：
    cd ~/.workbuddy/skills/ai-auto-test
    python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import report as R      # noqa: E402


def result(cases, defects=None):
    return {
        "summary": {"auto": len(cases), "passed": 0, "failed": len(cases),
                    "blocked": 0, "skipped": 0, "manual": 0, "flaky": 0,
                    "assertions_passed": 0, "assertions_total": 1,
                    "total": len(cases)},
        "target": {"base": "http://host", "env": "local", "auth_type": "none",
                   "credential_ok": True},
        "run": {"spec_name": "测试规格", "spec_version": "1", "started_at": "2026-10-20 17:00",
                "duration_ms": 1200, "id": "run-1", "spec_path": "spec.json"},
        "cases": cases,
        "defects": defects or [],
    }


def step(name="某步", status=None, passed=False, diagnostics=None):
    return {"name": name, "method": "UI:点击", "url": "http://host/x",
            "status": status, "elapsed_ms": 12, "passed": passed,
            "net_error": status == 0, "ui": True, "checks": [],
            "response_excerpt": "页面内容", "diagnostics": diagnostics or [],
            "screenshot": None}


def case(cid="TC-1", steps=None, module="模块A", title="用例标题",
         reason="不符", defect_level="P2"):
    return {"id": cid, "module": module, "title": title, "priority": "P0",
            "status": "failed", "flaky": False, "duration_ms": 100,
            "reason": reason, "basis": "", "defect_level": defect_level,
            "steps": steps or [step()], "assertions": {"passed": 0, "total": 1}}


class TestStatusRendering(unittest.TestCase):

    def test_no_navigation_shows_explicit_text(self):
        md = R.render_md(result([case()], [{"case": "TC-1", "module": "模块A",
                                            "level": "P2", "title": "用例标题",
                                            "reason": "不符"}]))
        self.assertIn("无导航", md)
        self.assertNotIn("HTTP None", md)

    def test_http_status_still_rendered(self):
        md = R.render_md(result([case(steps=[step(status=500)])],
                                [{"case": "TC-1", "module": "模块A", "level": "P2",
                                  "title": "用例标题", "reason": "不符"}]))
        self.assertIn("HTTP 500", md)

    def test_html_also_avoids_none(self):
        html = R.render_html(result([case()], [{"case": "TC-1", "module": "模块A",
                                                "level": "P2", "title": "用例标题",
                                                "reason": "不符"}]))
        self.assertIn("无导航", html)
        self.assertNotIn("HTTP None", html)

    def test_skipped_text_is_explained_not_blank(self):
        """没采集文本时说清原因。

        快照分级之后，不含文本断言的用例不再读整页 innerText。报告里如果
        只留一个空代码块，读的人会以为页面是空白 —— 这又是一种「看起来像
        结论的空白」。
        """
        s = step(status=None)
        s["response_excerpt"] = ""
        s["text_collected"] = False
        md = R.render_md(result([case(steps=[s])],
                                [{"case": "TC-1", "module": "模块A", "level": "P2",
                                  "title": "用例标题", "reason": "不符"}]))
        self.assertIn("未采集页面文本", md)
        self.assertNotIn("```json\n\n```", md)

    def test_empty_excerpt_without_ui_is_marked(self):
        s = step()
        s["ui"] = False
        s["response_excerpt"] = ""
        md = R.render_md(result([case(steps=[s])],
                                [{"case": "TC-1", "module": "模块A", "level": "P2",
                                  "title": "用例标题", "reason": "不符"}]))
        self.assertIn("（无内容）", md)


class TestDiagnosticsSection(unittest.TestCase):

    def test_section_absent_when_no_diagnostics(self):
        md = R.render_md(result([case(steps=[step(status=None)])]))
        self.assertNotIn("采集诊断", md)

    def test_section_present_with_diagnostics(self):
        c = case(steps=[step(diagnostics=[{"what": "失败截图没能保存（TC-1--x.png）",
                                           "error": "OSError: disk full"}])])
        md = R.render_md(result([c]))
        self.assertIn("采集诊断", md)
        self.assertIn("失败截图没能保存", md)
        self.assertIn("disk full", md)
        self.assertIn("不影响用例判定", md)

    def test_diagnostics_merged_across_cases(self):
        d = {"what": "networkidle 等待超时（8000ms）", "error": "Timeout"}
        cases = [case(cid="TC-1", steps=[step(diagnostics=[d])]),
                 case(cid="TC-2", steps=[step(diagnostics=[d])])]
        merged = R._collect_diagnostics(cases)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["cases"], ["TC-1", "TC-2"])

    def test_diagnostics_without_error_field(self):
        # 有些现象只说明情况、不带异常对象，渲染不能因此崩
        md = R.render_md(result([case(steps=[step(diagnostics=[{"what": "某现象"}])])]))
        self.assertIn("某现象", md)

    def test_html_section_present_with_diagnostics(self):
        c = case(steps=[step(diagnostics=[{"what": "事件监听器未能全部挂载"}])])
        html = R.render_html(result([c]))
        self.assertIn("采集诊断", html)
        self.assertIn("事件监听器未能全部挂载", html)

    def test_empty_what_is_ignored(self):
        merged = R._collect_diagnostics([case(steps=[step(diagnostics=[{"what": ""}])])])
        self.assertEqual(merged, [])


class TestReportStructure(unittest.TestCase):
    """改动章节编号时最容易出错：复现方式被挤掉、编号重复。"""

    def test_all_sections_present(self):
        md = R.render_md(result([case()]))
        for sec in ("结论", "总览", "按模块", "用例明细", "复现方式"):
            self.assertIn(sec, md)

    def test_reproduction_section_kept_after_diagnostics(self):
        c = case(steps=[step(diagnostics=[{"what": "某现象"}])])
        md = R.render_md(result([c]))
        self.assertIn("复现方式", md)
        self.assertLess(md.index("采集诊断"), md.index("复现方式"))

    def test_report_does_not_crash_on_empty_cases(self):
        md = R.render_md(result([]))
        self.assertIn("测试报告", md)


if __name__ == "__main__":
    unittest.main()
