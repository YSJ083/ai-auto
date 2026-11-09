"""报告渲染 —— 一份 Markdown（贴群/贴文档）+ 一份单文件 HTML（离线可看）。

报告的可信度比好看重要，所以固定包含五块「不能省」的内容：
  1. 环境与凭据状态（让人知道这轮结论是不是在有效环境下得出的）
  2. 分诊结果（通过 / 失败 / 阻断 / 跳过 / 手动，五类绝不混算）
  3. 不稳定项（flaky，比失败更该优先修）
  4. 失败项的实际响应摘要（能直接拿去复现）
  5. 本轮是哪一次运行（执行时间 + 执行编号，防止把旧快照当成本轮结论）
"""

import glob
import html
import json
import os
import re
import time

LABEL = {"passed": "通过", "failed": "失败", "blocked": "阻断", "skipped": "跳过", "manual": "手动"}
COLOR = {"passed": "#3B6D11", "failed": "#A32D2D", "blocked": "#854F0B", "skipped": "#5F5E5A", "manual": "#185FA5"}


def _pct(n, d):
    return "%.1f%%" % (n * 100.0 / d) if d else "—"


def _status_text(step):
    """步骤的状态描述。UI 步骤没有导航时本来就没有状态码，别显示成 None。"""
    st = step.get("status")
    return "HTTP %s" % st if st is not None else "无导航（UI 步骤，不产生状态码）"


def _excerpt_text(step, limit):
    """响应摘要。空的代码块会让人以为页面是空白，这里要说清是哪种空。"""
    text = (step.get("response_excerpt") or "")[:limit]
    if text:
        return text
    if step.get("ui") and step.get("text_collected") is False:
        return "（该用例没有文本类断言，本步骤未采集页面文本，属正常省略）"
    return "（无内容）"


def _download_text(step):
    """这一步下载到了什么。没有产物返回空串（不占版面）。"""
    items = step.get("downloads") or []
    if not items:
        return ""
    d = items[-1]
    size = d.get("size") or 0
    human = "%.1f KB" % (size / 1024.0) if size >= 1024 else "%d 字节" % size
    name = d.get("name") or "(未命名)"
    if d.get("error") and not d.get("path"):
        return "%s 保存失败：%s" % (name, d["error"])
    return "%s（%s）" % (name, human)


def _observability_text(step):
    """渲染只读网关采集到的脱敏观测证据。"""
    wait = step.get("observation_wait") or {}
    observations = step.get("observability") or {}
    if not wait and not observations:
        return ""
    parts = []
    if wait:
        state = "超时" if wait.get("超时") else ("阻断" if wait.get("阻断") else "已满足")
        parts.append("观测等待：来源 %s，%s，%s 次" %
                     (wait.get("来源", "—"), state, wait.get("次数", 0)))
    for source, observation in observations.items():
        if observation.get("unavailable"):
            parts.append("%s 不可用：%s" % (source, observation.get("reason", "原因未知")))
            continue
        summary = observation.get("summary") or {}
        parts.append("%s：%s %s，%s 条；安全摘要 %s" % (
            source, summary.get("kind", "观测"), summary.get("source", source),
            summary.get("record_count", 0),
            json.dumps(summary.get("records_sample", []), ensure_ascii=False)))
    return "；".join(parts)


    """按「现象」归并各步骤的诊断记录，同一现象只列一次。"""
    seen = {}
    for r in cases:
        for st in r.get("steps") or []:
            for d in st.get("diagnostics") or []:
                what = d.get("what") or ""
                if not what:
                    continue
                item = seen.setdefault(what, {"what": what, "error": d.get("error", ""),
                                              "cases": []})
                if r.get("id") and r["id"] not in item["cases"]:
                    item["cases"].append(r["id"])
    return list(seen.values())


def _collect_diagnostics(cases):
    """按「现象」归并各步骤的诊断记录，同一现象只列一次。"""
    seen = {}
    for result in cases:
        for step in result.get("steps") or []:
            for diagnostic in step.get("diagnostics") or []:
                what = diagnostic.get("what") or ""
                if not what:
                    continue
                item = seen.setdefault(what, {"what": what, "error": diagnostic.get("error", ""),
                                              "cases": []})
                if result.get("id") and result["id"] not in item["cases"]:
                    item["cases"].append(result["id"])
    return list(seen.values())


def _data_text(case):
    """数据驱动行的一句话说明。非数据驱动返回空串，调用处判断后再决定要不要写。"""
    d = case.get("data") or {}
    if not d:
        return ""
    vals = "、".join("%s=%s" % (k, v if isinstance(v, str) else str(v))
                     for k, v in (d.get("values") or {}).items())
    tail = "：" + vals if vals else ""
    return "数据组 %s/%s%s" % (d.get("index", "?"), d.get("total", "?"), tail)


def _data_bases(cases):
    return {r["data"]["base_id"] for r in cases if r.get("data")}


# 需求状态的措辞：和用例状态刻意不同 —— 需求没有「通过」这种说法，
# 只有「验证到了」和「没验证到」，否则读的人会把它当成又一张通过率表。
REQ_LABEL = {
    "passed": "已验证",
    "failed": "有失败",
    "blocked": "被阻断",
    "skipped": "未执行",
    "manual": "仅人工",
    "uncovered": "未覆盖",
}


def _coverage_headline(coverage):
    """结论里那一句覆盖度，直接决定读的人会不会以为「全绿=测全了」。"""
    if not coverage or not coverage.get("enabled"):
        return ""
    text = "需求覆盖 **%d/%d**" % (coverage["covered"], coverage["total"])
    text += "，已验证 %d 个" % coverage["verified"]
    if coverage["uncovered"]:
        text += "；**%d** 个需求点还没有对应用例（%s）—— 通过率再高，也不代表这些需求被测过" % (
            len(coverage["uncovered"]), "、".join(coverage["uncovered"][:8])
            + ("…" if len(coverage["uncovered"]) > 8 else ""))
    if coverage["failed"]:
        text += "；%d 个需求点下有失败用例" % len(coverage["failed"])
    return text + "。"


def _coverage_md(result):
    """需求覆盖章节的正文。标题由调用处加，章节号才会顺延。"""
    cov = result.get("coverage") or {}
    if not cov.get("enabled"):
        return []
    L = []
    if cov.get("partial_run"):
        L.append("> **本次是筛选或提前中断执行**（%s），下面的覆盖度只代表所选范围，不能当作全量覆盖汇报。"
                 % (("`--only %s`" % cov["filter"]) if cov.get("filter") else "--fail-fast 提前中断"))
        L.append("")
    if cov.get("declared_source") == "observed":
        L.append("> 规格里没有声明「需求」清单，分母取自用例里出现过的需求点。"
                 "这时覆盖 100% 只说明「标了需求点的都测了」，**不说明需求清单本身是完整的**。")
        L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append("| 需求点总数 | %d |" % cov["total"])
    L.append("| 有用例覆盖 | %d |" % cov["covered"])
    L.append("| 已验证（关联用例全部通过） | %d（%s） |" % (cov["verified"], _pct(cov["verified"], cov["total"])))
    L.append("| 无对应用例 | %d |" % len(cov["uncovered"]))
    L.append("| 有失败用例 | %d |" % len(cov["failed"]))
    L.append("| 仅人工验证 | %d |" % len(cov["manual"]))
    if cov.get("untracked_cases"):
        L.append("| 未标注需求点的用例 | %d |" % len(cov["untracked_cases"]))
    L.append("")
    L.append("| 需求点 | 说明 | 状态 | 关联用例 |")
    L.append("|---|---|---|---|")
    for it in cov["items"]:
        L.append("| `%s` | %s | %s | %s |" % (
            it["id"], it.get("note", "") or "—",
            REQ_LABEL.get(it["status"], it["status"]),
            "、".join(it["cases"][:4]) + ("…" if len(it["cases"]) > 4 else "") if it["cases"] else "—"))
    L.append("")
    L.append("> 口径：一条需求下所有关联用例都通过才算「已验证」；有任一条失败，整条需求记失败。"
             "「未覆盖」表示一条用例都没关联 —— 它和「通过了」是两件事。")
    L.append("")
    return L


def _group(results, key="module"):
    out = {}
    for r in results:
        out.setdefault(r.get(key) or "未分组", []).append(r)
    return out


# 章节序号按实际渲染顺延：规格没标需求点时「需求覆盖」整段不出现，
# 写死「四、用例明细」会留下一个空号，读的人会以为报告被截断了。
CN_NUM = list("一二三四五六七八九十")


def render_md(result):
    s = result["summary"]
    t = result["target"]
    run = result["run"]
    L = []
    n = [0]

    def sec(title):
        n[0] += 1
        L.append("## %s、%s" % (CN_NUM[min(n[0], len(CN_NUM)) - 1], title))
    L.append("# %s — 测试报告" % (run.get("spec_name") or "自动测试"))
    L.append("")
    L.append("| 项目 | 内容 |")
    L.append("|---|---|")
    L.append("| 规格版本 | v%s |" % (run.get("spec_version") or "?"))
    L.append("| 被测环境 | `%s`（%s） |" % (t["base"], t.get("env", "")))
    L.append("| 凭据方式 | %s　%s |" % (
        t.get("auth_type", "none"),
        "已就绪" if t.get("credential_ok") else "**未就绪**：" + (t.get("credential_error") or "")))
    L.append("| 执行时间 | %s（耗时 %.1fs） |" % (run["started_at"], run["duration_ms"] / 1000.0))
    L.append("| 执行编号 | `%s` |" % run["id"])
    L.append("")

    if not t.get("credential_ok"):
        L.append("> **本轮结论无效。** 凭据未就绪，用例全部未真正执行。先修凭据再重跑，不要把这里的红色当成缺陷。")
        L.append("")

    sec("结论")
    L.append("")
    verdict = []
    if s["failed"]:
        verdict.append("发现 **%d** 条缺陷候选" % s["failed"])
    if s["blocked"]:
        verdict.append("**%d** 条因环境/网络未执行完" % s["blocked"])
    if s["flaky"]:
        verdict.append("**%d** 条行为不稳定" % s["flaky"])
    if s["manual"]:
        verdict.append("%d 条需人工判定" % s["manual"])
    decided = s.get("decided", s["passed"] + s["failed"])
    L.append(("%s；已判定通过率 **%s**（%d/%d），自动执行完成率 **%s**（%d/%d）。" % (
        "，".join(verdict) if verdict else "全部通过",
        _pct(s["passed"], decided), s["passed"], decided,
        _pct(decided, s["total"]), decided, s["total"])))
    cov_head = _coverage_headline(result.get("coverage") or {})
    if cov_head:
        L.append("")
        L.append(cov_head)
    L.append("")

    sec("总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append("| 计划执行 | %d |" % s["total"])
    L.append("| 已判定（通过+失败） | %d |" % decided)
    L.append("| 已判定通过率 | %s |" % _pct(s["passed"], decided))
    L.append("| 自动执行完成率 | %s |" % _pct(decided, s["total"]))
    L.append("| 通过 | %d |" % s["passed"])
    L.append("| 失败（缺陷候选） | %d |" % s["failed"])
    L.append("| 阻断（环境问题） | %d |" % s["blocked"])
    L.append("| 跳过（依赖未通过） | %d |" % s["skipped"])
    L.append("| 手动/待人工 | %d |" % s["manual"])
    L.append("| 不稳定（重试后才过） | %d |" % s["flaky"])
    if s.get("data_rows"):
        # 数据驱动会把一条用例摊成多行，不说明的话「用例数怎么变多了」永远是个疑问
        L.append("| 数据驱动展开 | %d 行（来自 %d 条用例） |" % (
            s["data_rows"], len(_data_bases(result["cases"]))))
    L.append("| 断言命中 | %d/%d（%s） |" % (
        s["assertions_passed"], s["assertions_total"], _pct(s["assertions_passed"], s["assertions_total"])))
    L.append("")
    L.append("> 口径：`失败` 是「系统行为与规格定义的正确行为不符」，属于缺陷候选；"
             "`阻断` 是请求根本没完成，属于环境问题，两者不合并统计。"
             "通过率只算已判定的用例（通过+失败），阻断、跳过、待人工都不进分子分母；"
             "它们对计划数的影响看「自动执行完成率」。")
    L.append("")

    sec("按模块")
    L.append("")
    L.append("| 模块 | 用例 | 通过 | 失败 | 阻断 | 已判定通过率 |")
    L.append("|---|---|---|---|---|---|")
    for mod, rows in _group(result["cases"]).items():
        p = sum(1 for r in rows if r["status"] == "passed")
        f = sum(1 for r in rows if r["status"] == "failed")
        b = sum(1 for r in rows if r["status"] == "blocked")
        L.append("| %s | %d | %d | %d | %d | %s |" % (mod, len(rows), p, f, b, _pct(p, p + f)))
    L.append("")

    cov_lines = _coverage_md(result)
    if cov_lines:
        sec("需求覆盖")
        L.append("")
        L.extend(cov_lines)

    sec("用例明细")
    L.append("")
    L.append("| 用例ID | 模块 | 用例标题 | 优先级 | 结果 | 断言 |")
    L.append("|---|---|---|---|---|---|")
    for r in result["cases"]:
        a = r.get("assertions") or {}
        L.append("| %s | %s | %s | %s | **%s** | %s/%s |" % (
            r["id"], r.get("module", ""), r.get("title", ""), r.get("priority", ""),
            LABEL.get(r["status"], r["status"]), a.get("passed", 0), a.get("total", 0)))
    L.append("")

    if result["defects"]:
        sec("缺陷候选（%d 条）" % len(result["defects"]))
        L.append("")
        L.append("| # | 用例 | 模块 | 等级 | 期望行为 | 实际表现 |")
        L.append("|---|---|---|---|---|---|")
        for i, d in enumerate(result["defects"], 1):
            L.append("| %d | %s | %s | %s | %s | %s |" % (
                i, d["case"], d["module"], d["level"], d["title"],
                (d["reason"] or "").split("；")[0][:100].replace("|", "/")))
        L.append("")
        L.append("### 失败明细与证据")
        L.append("")
        for d in result["defects"]:
            r = next((x for x in result["cases"] if x["id"] == d["case"]), None)
            if not r:
                continue
            L.append("#### %s　%s" % (r["id"], r["title"]))
            L.append("")
            L.append("- 模块：%s　优先级：%s" % (r.get("module", ""), r.get("priority", "")))
            if _data_text(r):
                L.append("- %s" % _data_text(r))
            if r.get("defect_level"):
                L.append("- 等级说明：%s" % r["defect_level"])
            for one in (r.get("reason") or "").split("；"):
                if one:
                    L.append("- 不符项：`%s`" % one.replace("`", "'"))
            if r.get("basis"):
                L.append("- 判定依据：%s" % r["basis"])
            bad = next((x for x in r.get("steps") or [] if not x.get("passed")), None)
            if bad:
                L.append("- 请求：`%s %s`" % (bad["method"], bad["url"]))
                L.append("- 响应：%s · %sms" % (_status_text(bad), bad["elapsed_ms"]))
                art = _download_text(bad)
                if art:
                    L.append("- 下载产物：%s" % art)
                observation = _observability_text(bad)
                if observation:
                    L.append("- 观测证据：%s" % observation)
                L.append("")
                L.append("```json")
                L.append(_excerpt_text(bad, 600))
                L.append("```")
            L.append("")

    if s["blocked"] or s["skipped"]:
        sec("未执行完的用例")
        L.append("")
        for r in result["cases"]:
            if r["status"] in ("blocked", "skipped"):
                dt = _data_text(r)
                L.append("- **%s** %s（%s）：%s%s" % (
                    r["id"], r["title"], LABEL[r["status"]], r.get("reason", "")[:200],
                    ("　·　" + dt) if dt else ""))
        L.append("")

    flaky = [r for r in result["cases"] if r.get("flaky")]
    if flaky:
        sec("不稳定项（优先于失败处理）")
        L.append("")
        for r in flaky:
            L.append("- **%s** %s：%s" % (r["id"], r["title"], r.get("reason", "")))
        L.append("")

    diag = _collect_diagnostics(result["cases"])
    if diag:
        sec("采集诊断（不影响用例判定）")
        L.append("")
        L.append("> 这些是采集、等待环节没走顺的记录，**不是被测系统的缺陷**。"
                 "但其中「截图未能保存」「事件监听器未挂载」出现时，本轮证据不完整，"
                 "看结论时要打折扣。")
        L.append("")
        L.append("| 现象 | 涉及用例 | 细节 |")
        L.append("|---|---|---|")
        for d in diag:
            L.append("| %s | %s | %s |" % (
                d["what"], "、".join(d["cases"][:6]) or "—",
                (d["error"] or "—").replace("|", "/")[:120]))
        L.append("")

    sec("复现方式")
    L.append("")
    L.append("```bash")
    L.append("# 全量")
    L.append("python3 scripts/auto_test.py run %s --env %s" % (
        os.path.basename(run.get("spec_path", "spec.json")), result["target"].get("env", "")))
    if run.get("filter"):
        L.append("# 本轮只跑了前缀 %s" % run["filter"])
    L.append("```")
    L.append("")
    L.append("> 报告由执行器自动生成，未做人工润色。失败项是否成立，仍需人确认业务预期。")
    return "\n".join(L)


def _coverage_html(result):
    """HTML 版需求覆盖。与 md 同源同口径，只是排版不同。"""
    cov = result.get("coverage") or {}
    if not cov.get("enabled"):
        return ""
    esc = html.escape
    warn = ""
    if cov.get("partial_run"):
        warn += ('<p class="hint">本次是筛选或提前中断执行（%s），覆盖度只代表所选范围。</p>'
                 % esc(("--only %s" % cov["filter"]) if cov.get("filter") else "--fail-fast 提前中断"))
    if cov.get("declared_source") == "observed":
        warn += ('<p class="hint">规格未声明「需求」清单，分母取自用例里出现过的需求点：'
                 '覆盖 100% 只说明标了需求点的都测了，不说明需求清单完整。</p>')
    head = ('<tr><td>需求点总数</td><td class="mono">%d</td></tr>'
            '<tr><td>有用例覆盖</td><td class="mono">%d</td></tr>'
            '<tr><td>已验证（关联用例全部通过）</td><td class="mono">%d（%s）</td></tr>'
            '<tr><td>无对应用例</td><td class="mono">%d</td></tr>'
            '<tr><td>有失败用例</td><td class="mono">%d</td></tr>'
            '<tr><td>仅人工验证</td><td class="mono">%d</td></tr>'
            % (cov["total"], cov["covered"], cov["verified"], _pct(cov["verified"], cov["total"]),
               len(cov["uncovered"]), len(cov["failed"]), len(cov["manual"])))
    if cov.get("untracked_cases"):
        head += ('<tr><td>未标注需求点的用例</td><td class="mono">%d</td></tr>'
                 % len(cov["untracked_cases"]))
    rows = "".join(
        '<tr><td class="mono">%s</td><td>%s</td><td><span class="chip c-%s">%s</span></td>'
        '<td class="mono">%s</td></tr>'
        % (esc(it["id"]), esc(it.get("note", "") or "—"), esc(it["status"]),
           esc(REQ_LABEL.get(it["status"], it["status"])),
           esc("、".join(it["cases"][:4]) + ("…" if len(it["cases"]) > 4 else "") or "—"))
        for it in cov["items"])
    return ('<section><h2>需求覆盖</h2>%s'
            '<table><thead><tr><th>指标</th><th>数值</th></tr></thead><tbody>%s</tbody></table>'
            '<table><thead><tr><th>需求点</th><th>说明</th><th>状态</th><th>关联用例</th></tr></thead>'
            '<tbody>%s</tbody></table>'
            '<p class="hint">一条需求下所有关联用例都通过才算「已验证」；有任一条失败，整条需求记失败。'
            '「未覆盖」表示一条用例都没关联 —— 它和「通过了」是两件事。</p></section>'
            % (warn, head, rows))


def render_html(result):
    s = result["summary"]
    t = result["target"]
    run = result["run"]
    esc = html.escape

    def chip(status):
        return '<span class="chip c-%s">%s</span>' % (status, LABEL.get(status, status))

    rows = []
    for r in result["cases"]:
        a = r.get("assertions") or {}
        detail = ""
        if _data_text(r):
            # 数据驱动：同一个标题会出现多次，先把「这行用的哪组参数」写在标题下面，
            # 否则读的人会以为是重复执行
            detail = '<div class="req">%s</div>' % esc(_data_text(r))
        if r.get("reason"):
            detail += '<div class="reason">%s</div>' % esc(r["reason"][:400])
        bad = next((x for x in r.get("steps") or [] if not x.get("passed")), None)
        body = ""
        if bad:
            art = _download_text(bad)
            observation = _observability_text(bad)
            body = ('<details><summary>响应证据</summary><pre>%s</pre>'
                    '<div class="req">%s %s → %s · %sms</div>%s%s</details>'
                    % (esc(_excerpt_text(bad, 1200)), esc(bad["method"]),
                       esc(bad["url"]), esc(_status_text(bad)), bad["elapsed_ms"],
                       ('<div class="req">下载产物：%s</div>' % esc(art)) if art else "",
                       ('<div class="req">观测证据：%s</div>' % esc(observation)) if observation else ""))
        rows.append(
            '<tr><td class="mono">%s</td><td>%s</td><td>%s%s%s</td><td class="mono">%s</td>'
            '<td>%s</td><td class="mono">%s/%s</td></tr>'
            % (esc(r["id"]), esc(r.get("module", "")), esc(r.get("title", "")), detail, body,
               esc(r.get("priority", "")), chip(r["status"]),
               a.get("passed", 0), a.get("total", 0)))

    mod_rows = []
    for mod, listrows in _group(result["cases"]).items():
        p = sum(1 for x in listrows if x["status"] == "passed")
        f = sum(1 for x in listrows if x["status"] == "failed")
        b = sum(1 for x in listrows if x["status"] == "blocked")
        mod_rows.append("<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%s</td></tr>"
                        % (esc(mod), len(listrows), p, f, b, _pct(p, p + f)))

    defect_rows = []
    for i, d in enumerate(result["defects"], 1):
        defect_rows.append(
            '<tr><td class="mono">%d</td><td class="mono">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
            % (i, esc(d["case"]), esc(d["module"]), esc(d["level"]), esc(d["title"]),
               esc((d["reason"] or "")[:220])))

    flaky = [r for r in result["cases"] if r.get("flaky")]
    flaky_html = ""
    if flaky:
        items = "".join('<li><b>%s</b> %s：%s</li>' % (esc(r["id"]), esc(r["title"]), esc(r.get("reason", "")))
                        for r in flaky)
        flaky_html = '<section><h2>不稳定项</h2><p class="hint">重试后才通过，说明行为不确定，优先排查。</p><ul>%s</ul></section>' % items

    warn = ""
    if not t.get("credential_ok"):
        warn = ('<div class="warn"><b>本轮结论无效</b>　凭据未就绪：%s。'
                '未真正执行的用例不应算作缺陷。</div>' % esc(t.get("credential_error", "")))

    diag = _collect_diagnostics(result["cases"])
    diag_html = ""
    if diag:
        items = "".join(
            '<li><b>%s</b>　涉及 %s　%s</li>'
            % (esc(d["what"]), esc("、".join(d["cases"][:6]) or "—"),
               esc((d["error"] or "—")[:200]))
            for d in diag)
        diag_html = ('<section><h2>采集诊断（不影响判定）</h2>'
                     '<p class="hint">采集、等待环节没走顺的记录，不是被测系统的缺陷；'
                     '但出现「截图未能保存」「监听器未挂载」时，本轮证据不完整。</p>'
                     '<ul class="hint">%s</ul></section>' % items)

    # 需求覆盖卡片：规格没写需求点就整块不出现，老规格的报告保持原样
    cov = result.get("coverage") or {}
    cov_card = ""
    if cov.get("enabled"):
        cov_card = ('<div class="card"><div class="k">需求覆盖</div><div class="v">%d/%d</div>'
                    '<div class="d">已验证 %d 个%s</div></div>'
                    % (cov["covered"], cov["total"], cov["verified"],
                       ("，%d 个未覆盖" % len(cov["uncovered"])) if cov["uncovered"] else ""))

    return """<div class="wrap">
<h1>{name}<span class="ver">v{ver}</span></h1>
<div class="meta">
<div><span>被测环境</span><code>{base}</code></div>
<div><span>环境档</span>{env}</div>
<div><span>凭据</span>{auth}</div>
<div><span>执行时间</span>{started} · {dur}s</div>
<div><span>执行编号</span><code>{rid}</code></div>
</div>
{warn}
<div class="cards">
<div class="card"><div class="k">已判定通过率</div><div class="v">{rate}</div><div class="d">{passed}/{decided} 条已判定</div></div>
<div class="card"><div class="k">自动执行完成率</div><div class="v">{crate}</div><div class="d">{decided}/{total} 条得出结论</div></div>
<div class="card"><div class="k">缺陷候选</div><div class="v c-failed">{failed}</div><div class="d">需人确认业务预期</div></div>
<div class="card"><div class="k">环境阻断</div><div class="v c-blocked">{blocked}</div><div class="d">不计入缺陷</div></div>
<div class="card"><div class="k">待人工</div><div class="v c-manual">{manual}</div><div class="d">未自动判定</div></div>
<div class="card"><div class="k">断言命中</div><div class="v">{aok}/{aall}</div><div class="d">覆盖 {acases} 条用例</div></div>
{data_card}
{cov_card}
</div>
<section><h2>按模块</h2>
<table><thead><tr><th>模块</th><th>用例</th><th>通过</th><th>失败</th><th>阻断</th><th>已判定通过率</th></tr></thead>
<tbody>{mods}</tbody></table></section>
{cov}
{defects}
{flaky}
{diag}
<section><h2>用例明细</h2>
<table><thead><tr><th>用例ID</th><th>模块</th><th>用例标题</th><th>优先级</th><th>结果</th><th>断言</th></tr></thead>
<tbody>{rows}</tbody></table></section>
<section><h2>口径说明</h2>
<ul class="hint">
<li><b>失败</b>：系统行为与规格定义的正确行为不符，属缺陷候选，仍需人确认业务预期。</li>
<li><b>阻断</b>：请求未完成（连不上、超时、凭据失效），属环境问题，不计入缺陷。</li>
<li><b>跳过</b>：依赖的用例没通过，本条未执行，不参与通过率。</li>
<li>报告由执行器自动生成，未做人工润色。</li>
</ul></section>
</div>
<style>
.wrap{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;color:#2C2C2A;max-width:1080px;margin:0 auto;padding:8px 4px 40px;line-height:1.65}}
h1{{font-size:22px;font-weight:600;margin:0 0 4px}}
h1 .ver{{font-size:13px;font-weight:400;color:#5F5E5A;margin-left:8px}}
h2{{font-size:15px;font-weight:600;margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid #D3D1C7}}
.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:4px 20px;font-size:12.5px;color:#5F5E5A;margin:12px 0 18px}}
.meta span{{display:inline-block;min-width:62px;color:#888780}}
.meta code{{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#F1EFE8;padding:1px 5px;border-radius:4px;color:#2C2C2A}}
.warn{{background:#FAEEDA;border:1px solid #EF9F27;border-radius:8px;padding:10px 14px;font-size:13px;margin:0 0 16px;color:#412402}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:6px 0 4px}}
.card{{border:1px solid #D3D1C7;border-radius:10px;padding:12px 14px;background:#fff}}
.card .k{{font-size:12px;color:#5F5E5A}}
.card .v{{font-size:24px;font-weight:600;margin:2px 0 0;font-variant-numeric:tabular-nums}}
.card .d{{font-size:11.5px;color:#888780}}
.v.c-failed{{color:#A32D2D}} .v.c-blocked{{color:#854F0B}} .v.c-manual{{color:#185FA5}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}}
th{{text-align:left;font-weight:500;color:#5F5E5A;border-bottom:1px solid #D3D1C7;padding:7px 8px;background:#F1EFE8}}
td{{border-bottom:1px solid #EFEDE6;padding:7px 8px;vertical-align:top}}
tr:hover td{{background:#FAF9F5}}
.mono{{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;white-space:nowrap}}
.chip{{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:20px;border:1px solid}}
.c-passed{{color:#3B6D11;border-color:#97C459;background:#EAF3DE}}
.c-failed{{color:#A32D2D;border-color:#F09595;background:#FCEBEB}}
.c-blocked{{color:#854F0B;border-color:#EF9F27;background:#FAEEDA}}
.c-skipped{{color:#5F5E5A;border-color:#B4B2A9;background:#F1EFE8}}
.c-manual{{color:#185FA5;border-color:#85B7EB;background:#E6F1FB}}
.c-uncovered{{color:#8A3B12;border-color:#E5B594;background:#FDF0E6}}
.reason{{font-size:11.5px;color:#A32D2D;margin-top:3px;max-width:520px}}
details{{margin-top:3px}} summary{{font-size:11.5px;color:#185FA5;cursor:pointer}}
pre{{font-family:ui-monospace,Menlo,monospace;font-size:11px;background:#F1EFE8;padding:8px 10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;max-height:260px}}
.req{{font-size:11px;color:#5F5E5A;font-family:ui-monospace,monospace;margin-top:2px}}
.hint{{font-size:12.5px;color:#5F5E5A}}
ul.hint li{{margin:3px 0}}
</style>""".format(
        name=esc(run.get("spec_name") or "自动测试"),
        ver=esc(str(run.get("spec_version") or "?")),
        base=esc(t["base"]),
        env=esc(t.get("env", "")),
        auth=esc("%s · %s" % (t.get("auth_type", "none"), "已就绪" if t.get("credential_ok") else "未就绪")),
        started=esc(run["started_at"]),
        dur="%.1f" % (run["duration_ms"] / 1000.0),
        rid=esc(run["id"]),
        warn=warn,
        rate=_pct(s["passed"], s.get("decided", s["passed"] + s["failed"])),
        crate=_pct(s.get("decided", s["passed"] + s["failed"]), s["total"]),
        passed=s["passed"], auto=s["auto"],
        decided=s.get("decided", s["passed"] + s["failed"]), total=s["total"],
        failed=s["failed"], blocked=s["blocked"], manual=s["manual"],
        aok=s["assertions_passed"], aall=s["assertions_total"], acases=s["total"],
        data_card=('<div class="card"><div class="k">数据驱动</div><div class="v">%d</div>'
                   '<div class="d">行，来自 %d 条用例</div></div>'
                   % (s["data_rows"], len(_data_bases(result["cases"]))))
        if s.get("data_rows") else "",
        cov_card=cov_card,
        cov=_coverage_html(result),
        mods="".join(mod_rows),
        defects=('<section><h2>缺陷候选</h2><table><thead><tr><th>#</th><th>用例</th><th>模块</th>'
                 '<th>等级</th><th>期望行为</th><th>实际表现</th></tr></thead><tbody>%s</tbody></table></section>'
                 % "".join(defect_rows)) if defect_rows else "",
        flaky=flaky_html,
        diag=diag_html,
        rows="".join(rows),
    )


def _prune_snapshots(snap_dir, keep=20):
    """历史目录只留最近 keep 组快照，更旧的删掉最老的那批。

    这里是 `历史/` 内部，删的是已经被更新的快照取代的旧档；keep 给 20，
    正常项目很难跑满。
    """
    groups = {}
    for p in glob.glob(os.path.join(snap_dir, "测试报告_*")):
        m = re.match(r"测试报告_(.+)\.(md|html)$", os.path.basename(p))
        if m:
            groups.setdefault(m.group(1), []).append(p)
    stamps = sorted(groups)
    if len(stamps) <= keep:
        return []

    removed = []
    for st in stamps[:-keep]:
        for p in groups[st]:
            os.remove(p)
            removed.append(os.path.basename(p))
    return removed


def write_reports(result, out_dir, stamp):
    """顶层只写一份「最新报告」，每次运行的快照进 `历史/`。

    以前是「固定名 + 时间戳快照」双写顶层，结果是同一次运行产出两个内容
    相同的 html，而跑三轮就堆出六份，文件名只差一串时间数字。人打开目录
    时分不清哪份是这轮结论，很容易对着上一轮的失败结果做判断。

    现在顶层永远只有一个入口：`最新报告.md` / `最新报告.html`；要看历史
    就进 `历史/`，按时间戳排得整整齐齐。
    """
    md = render_md(result)
    snap_dir = os.path.join(out_dir, "历史")
    os.makedirs(snap_dir, exist_ok=True)

    with open(os.path.join(snap_dir, "测试报告_%s.md" % stamp), "w", encoding="utf-8") as f:
        f.write(md)
    md_path = os.path.join(out_dir, "最新报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    run = result["run"]
    name = run.get("spec_name") or "自动测试"

    def _wrap(inner):
        # 标题里带上执行时间，浏览器多标签并排时也能分清哪一份是最新
        return ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>%s 测试报告 · %s</title></head><body style=\"margin:0;padding:24px;"
                "background:#FAF9F5\">%s</body></html>"
                % (html.escape(name), html.escape(run.get("started_at", "")), inner))

    try:
        page = _wrap(render_html(result))
        with open(os.path.join(snap_dir, "测试报告_%s.html" % stamp), "w", encoding="utf-8") as f:
            f.write(page)
        html_path = os.path.join(out_dir, "最新报告.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page)
    except Exception:
        html_path = ""
    _prune_snapshots(snap_dir)
    return md_path, html_path
