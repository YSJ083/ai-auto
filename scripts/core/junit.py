"""JUnit XML 输出 —— 让执行结果直接进 CI 的测试报表。

为什么单独一个模块：md/html 是给人读的（需要叙述、证据、诊断），
junit.xml 是给 Jenkins / GitLab / Azure 读的（只认机读结构）。两套取舍不同，
塞在一个渲染器里会互相牵制 —— 改报告排版时容易碰坏 CI 的字段。

状态映射（这条不能随意改，改了会直接改变门禁行为）：
    passed  -> 空 <testcase/>
    failed  -> <failure/>   断言不通过，被测系统的行为问题
    blocked -> <error/>     环境问题（连不上 / 凭据失效）

    阻断刻意映射成 error 而不是 skipped：skipped 在多数 CI 里显示为「跳过」，
    不会让流水线变红，等于把「这轮根本没测成」伪装成「不需要测」。
    退出码 3 和这个 error 说的是同一件事，两边保持一致。
"""

from xml.sax.saxutils import escape, quoteattr

# XML 1.0 不允许的控制字符（响应体里偶尔会有），留着会让 CI 解析时报
# "not well-formed"。这里统一换成空格，不丢结构。
_BAD_CHARS = dict.fromkeys(
    [c for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)], None)


def _clean(s):
    return ("" if s is None else str(s)).translate(_BAD_CHARS)


def _attr(v):
    """属性值：先清控制字符再转义，引号由 quoteattr 自己选。"""
    return quoteattr(_clean(v))


def _secs(ms):
    try:
        return "%.3f" % (float(ms) / 1000.0)
    except (TypeError, ValueError):
        return "0.000"


def _failed_checks(case):
    """把失败断言整理成可读明细，贴进缺陷单不用再翻报告。"""
    out = []
    for st in case.get("steps") or []:
        for chk in st.get("checks") or []:
            if chk.get("passed"):
                continue
            out.append("%s · %s\n  期望：%s\n  实际：%s" % (
                st.get("name", ""), chk.get("type", ""),
                chk.get("expected", ""), chk.get("actual", "")))
    if not out and case.get("reason"):
        out.append(case["reason"])
    return "\n".join(out)


def _case_xml(case):
    cid = case.get("id", "")
    title = case.get("title", "")
    name = ("%s %s" % (cid, title)).strip()
    status = case.get("status", "")
    tm = _secs(case.get("duration_ms"))
    head = "<testcase classname=%s name=%s time=%s" % (
        _attr(case.get("module") or "未分组"), _attr(name), _attr(tm))
    reason = _clean(case.get("reason", ""))

    if status == "failed":
        body = _clean(_failed_checks(case))
        return "%s>\n      <failure message=%s type=\"assertion\">%s</failure>\n    </testcase>" % (
            head, _attr(reason[:500]), escape(body[:4000]))
    if status == "blocked":
        return "%s>\n      <error message=%s type=\"environment\">%s</error>\n    </testcase>" % (
            head, _attr(reason[:500]), escape(reason[:2000]))
    if status == "skipped":
        return "%s>\n      <skipped message=%s/>\n    </testcase>" % (
            head, _attr(reason[:300] or "依赖的用例未通过"))
    if status == "manual":
        return "%s>\n      <skipped message=%s/>\n    </testcase>" % (
            head, _attr("需要人工判定：" + (reason[:200] or "未说明")))
    return "%s/>" % head


def _suite_xml(module, cases, tool, env, base):
    counts = {"failed": 0, "blocked": 0, "skipped": 0, "manual": 0}
    for c in cases:
        s = c.get("status")
        if s in counts:
            counts[s] += 1
    time_s = sum(float(c.get("duration_ms") or 0) for c in cases) / 1000.0
    out = ['  <testsuite name=%s tests="%d" failures="%d" errors="%d" skipped="%d" time="%.3f">'
           % (_attr(module), len(cases), counts["failed"], counts["blocked"],
              counts["skipped"] + counts["manual"], time_s)]
    # properties 让 CI 页面直接看出这批结果跑的是哪个环境、哪个工具版本
    out.append("    <properties>")
    out.append('      <property name="tool" value=%s/>' % _attr(tool))
    out.append('      <property name="env" value=%s/>' % _attr(env))
    out.append('      <property name="base" value=%s/>' % _attr(base))
    out.append("    </properties>")
    for c in cases:
        out.append("    " + _case_xml(c))
    out.append("  </testsuite>")
    return "\n".join(out)


def render_junit(result):
    """result.json 的结构 → JUnit XML 字符串。"""
    run = result.get("run") or {}
    target = result.get("target") or {}
    tool = "%s %s" % ((result.get("tool") or {}).get("name", ""),
                      (result.get("tool") or {}).get("version", ""))
    cases = result.get("cases") or []

    by_module = {}
    for c in cases:
        by_module.setdefault(c.get("module") or "未分组", []).append(c)

    failed = len([c for c in cases if c.get("status") == "failed"])
    blocked = len([c for c in cases if c.get("status") == "blocked"])
    skipped = len([c for c in cases if c.get("status") in ("skipped", "manual")])
    total_s = float(run.get("duration_ms") or 0) / 1000.0
    stamp = (run.get("started_at") or "").replace(" ", "T")

    out = ['<?xml version="1.0" encoding="UTF-8"?>']
    out.append('<testsuites name=%s tests="%d" failures="%d" errors="%d" skipped="%d" time="%.3f">'
               % (_attr(run.get("spec_name") or "autotest"), len(cases), failed,
                  blocked, skipped, total_s))
    for module in sorted(by_module):
        out.append(_suite_xml(module, by_module[module], tool,
                              target.get("env", ""), target.get("base", "")))
        if module == sorted(by_module)[0] and stamp:
            # timestamp 只挂在第一个 suite 上：JUnit 允许多个，但重复值没意义
            out[-1] = out[-1].replace(
                ">", ' timestamp="%s">' % escape(stamp), 1)
    out.append("</testsuites>")
    return "\n".join(out) + "\n"


def write_junit(result, path):
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_junit(result))
    return path
