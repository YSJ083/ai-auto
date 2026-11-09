import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import merge_results as M


def _dump(tmpdir, name, payload):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


AUTO_TEST_PAYLOAD = {
    "tool": {"name": "ai-auto-test", "version": "2.0.0"},
    "run": {"id": "run-abc"},
    "target": {"env": "test", "base": "http://127.0.0.1:8899"},
    "summary": {},
    "cases": [
        {"id": "TC-API-1", "module": "用户", "title": "读用户", "priority": "P0",
         "status": "passed", "reason": "", "assertions": {"passed": 3, "total": 3}, "steps": []},
        {"id": "TC-API-2", "module": "订单", "title": "分页", "priority": "P0",
         "status": "failed", "reason": "pageSize=5", "assertions": {"passed": 2, "total": 3}, "steps": []},
        {"id": "TC-API-3", "module": "订单", "title": "断网", "priority": "P1",
         "status": "blocked", "reason": "连接失败", "assertions": {"passed": 0, "total": 0}, "steps": []},
    ],
}

AGENT_PAYLOAD = {
    "skill_version": "2.0.0",
    "batch_id": "batch-xyz",
    "target": "mock 回放桩",
    "summary": {},
    "results": [
        {"id": "TC-AG-1", "用例标题": "查工单", "所属模块": "工单", "优先级": "P0",
         "判定": "通过", "判定方式": ["硬断言"], "预期结果": "返回工单号",
         "通过次数": 1, "失败次数": 0,
         "证据": {"结果文件": "result.json", "路径": "results[0].runs",
                  "trace_available": True, "事件数": 2},
         "runs": [{"output": "INC-1", "reason": "关键词命中", "trace_available": True, "events": []}]},
        {"id": "TC-AG-2", "用例标题": "多轮", "所属模块": "工单", "优先级": "P1",
         "判定": "不稳定", "判定方式": ["硬断言", "模型评审"],
         "通过次数": 1, "失败次数": 1,
         "runs": [{"output": "x", "reason": "抖动", "trace_available": False, "events": []}]},
        {"id": "TC-AG-3", "用例标题": "敏感", "所属模块": "安全", "优先级": "P1",
         "判定": "待人工", "判定方式": ["人工"],
         "通过次数": 0, "失败次数": 0,
         "runs": [{"output": "", "reason": "需人工确认", "trace_available": False, "events": []}]},
    ],
}


class MergeTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.api = _dump(self.tmp, "api.json", AUTO_TEST_PAYLOAD)
        self.agent = _dump(self.tmp, "agent.json", AGENT_PAYLOAD)

    def test_detect_format(self):
        self.assertEqual("ai-auto-test", M.detect_format(AUTO_TEST_PAYLOAD))
        self.assertEqual("testcase-designer", M.detect_format(AGENT_PAYLOAD))
        self.assertEqual("", M.detect_format({"foo": 1}))

    def test_status_mapping_and_unified_fields(self):
        _, api_rows = M.load_rows(self.api)
        _, agent_rows = M.load_rows(self.agent)
        self.assertEqual(["passed", "failed", "blocked"], [r["status"] for r in api_rows])
        self.assertEqual(["passed", "flaky", "manual"], [r["status"] for r in agent_rows])
        self.assertEqual(sorted(api_rows[0].keys()), sorted(agent_rows[0].keys()))

    def test_decision_method_carried_from_agent(self):
        _, agent_rows = M.load_rows(self.agent)
        self.assertEqual("硬断言", agent_rows[0]["decision_method"])
        self.assertEqual("硬断言、模型评审", agent_rows[1]["decision_method"])
        self.assertEqual("人工", agent_rows[2]["decision_method"])

    def test_trace_availability_is_not_inferred(self):
        _, api_rows = M.load_rows(self.api)
        self.assertIsNone(api_rows[0]["evidence"]["trace_available"])

    def test_summary_excludes_blocked_and_manual_from_denominator(self):
        rows = M.load_rows(self.api)[1] + M.load_rows(self.agent)[1]
        s = M.summarize(rows)
        self.assertEqual(6, s["用例总数"])
        self.assertEqual(2, s["阻断"] + s["待人工"])
        # 通过 2，失败 1，不稳定 1 → 已判定 4 条
        self.assertEqual(0.5, s["已判定通过率"])
        self.assertEqual(round(4 / 6, 3), s["自动执行完成率"])
        self.assertEqual(1, s["P0失败"])

    def test_baseline_keyed_by_source_and_id(self):
        merged = {"cases": [{"source": "ai-auto-test", "case_id": "TC-API-1",
                             "title": "读用户", "status": "passed"},
                            {"source": "ai-auto-test", "case_id": "TC-API-2",
                             "title": "分页", "status": "failed"}]}
        base = _dump(self.tmp, "base.json", merged)
        current = [
            {"source": "ai-auto-test", "case_id": "TC-API-1", "title": "读用户", "status": "failed"},
            {"source": "ai-auto-test", "case_id": "TC-API-2", "title": "分页", "status": "passed"},
        ]
        diff = M.diff_baseline(current, base)
        self.assertEqual(["[ai-auto-test] 读用户"], diff["新增失败"])
        self.assertEqual(["[ai-auto-test] 分页"], diff["已修复"])

    def test_legacy_rows_without_id_do_not_collapse(self):
        """旧结果没有 ID：必须逐条兜底，不能都变成空串在映射里塌缩。"""
        legacy = {"results": [{"用例标题": "旧A", "判定": "通过"},
                              {"用例标题": "旧B", "判定": "失败"}]}
        path = _dump(self.tmp, "legacy.json", legacy)
        _, rows = M.load_rows(path)
        self.assertEqual(["LEGACY-001", "LEGACY-002"], [r["case_id"] for r in rows])
        self.assertEqual(2, len({r["case_id"] for r in rows}))

    def test_merge_redacts_secrets_from_source(self):
        payload = {"results": [{"id": "TC-1", "用例标题": "t", "判定": "失败",
                                "runs": [{"reason": "Authorization: Bearer abc.def token=sk-1"}]}]}
        path = _dump(self.tmp, "secret.json", payload)
        _, rows = M.load_rows(path)
        blob = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("abc.def", blob)
        self.assertNotIn("sk-1", blob)
        self.assertIn("[REDACTED]", blob)

    def test_gate_only_applies_when_configured(self):
        rows = M.load_rows(self.api)[1] + M.load_rows(self.agent)[1]
        s = M.summarize(rows)
        self.assertEqual([], M.gate_hits(s, []))
        self.assertTrue(M.gate_hits(s, ["blocked"]))
        self.assertTrue(M.gate_hits(s, ["manual"]))
        self.assertTrue(M.gate_hits(s, ["p0"]))


if __name__ == "__main__":
    unittest.main()
