#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨引擎结果汇总：把 ai-auto-test 与 testcase-designer 的 result.json 合成一份。

两个 Skill 的执行通道不同（HTTP/Web 断言 vs Agent 对话评测），但结论必须能放在
一张表里比较。本脚本只做三件事：识别来源、归一化字段与状态、统一口径汇总。
不重跑用例，不推断缺失信息——源结果里没有的，这里标记为「未采集」。

用法:
    python3 merge_results.py api/result.json agent/result.json --out ./汇总结果
    python3 merge_results.py a.json b.json --baseline 上一轮_merged.json

退出码: 0 = 无失败/不稳定；1 = 存在失败或不稳定；2 = 输入不可用
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.redact import configure as configure_redaction, redact  # noqa: E402

VERSION = "2.0.0"

# 统一状态：passed / failed / flaky / blocked / skipped / manual
STATUS_LABEL = {
    "passed": "通过",
    "failed": "失败",
    "flaky": "不稳定",
    "blocked": "阻断",
    "skipped": "跳过",
    "manual": "待人工",
}

AGENT_STATUS = {
    "通过": "passed",
    "失败": "failed",
    "不稳定": "flaky",
    "阻断": "blocked",
    "待人工": "manual",
}

DECIDED = ("passed", "failed", "flaky")


def detect_format(data):
    """按结构识别来源，不依赖文件名。"""
    if isinstance(data, dict) and "cases" in data and "run" in data:
        return "ai-auto-test"
    if isinstance(data, dict) and "results" in data:
        return "testcase-designer"
    return ""


def _case_id(raw, i):
    """用例 ID 缺失时的兜底。

    旧版结果没有稳定 ID，不兜底会让所有行都变成空串，在基线映射里塌缩成一条——
    用例凭空消失，比对比失败更危险。这里给按位置生成的可读 ID，
    并在文档里注明：顺序变化会导致对比错位。
    """
    return raw or f"LEGACY-{i + 1:03d}"


def normalize_auto_test(data, path):
    """ai-auto-test：断言驱动，状态已是英文枚举。"""
    run = data.get("run") or {}
    target = data.get("target") or {}
    version = (data.get("tool") or {}).get("version", "")
    rows = []
    for i, c in enumerate(data.get("cases") or []):
        rows.append({
            "batch_id": run.get("id", ""),
            "source": "ai-auto-test",
            "channel": "ui" if any(isinstance(s, dict) and (s.get("ui") or s.get("动作"))
                                   for s in c.get("steps") or []) else "api",
            "case_id": _case_id(c.get("id", ""), i),
            "title": c.get("title", ""),
            "module": c.get("module", ""),
            "priority": c.get("priority", ""),
            "status": c.get("status", ""),
            "decision_method": "硬断言",
            "expected": c.get("basis", "") or c.get("预期结果", ""),
            "actual": c.get("reason", ""),
            "reason": c.get("reason", ""),
            "retried": bool(c.get("retried")),
            "evidence": {
                "file": os.path.basename(path),
                "path": f"cases[{i}].steps",
                "trace_available": None,
                "detail": {"assertions": c.get("assertions") or {}},
            },
            "version": version,
            "target": target.get("base", ""),
            "env": target.get("env", ""),
        })
    return rows


def _agent_evidence(ev, path, i):
    """Agent 结果里的证据块字段名是中文，这里统一成与 API 通道一致的键。"""
    ev = ev or {}
    return {
        "file": ev.get("结果文件") or os.path.basename(path),
        "path": ev.get("路径") or f"results[{i}].runs",
        "trace_available": ev.get("trace_available"),
        "detail": {"事件数": ev.get("事件数", 0)},
    }


def normalize_agent(data, path):
    """testcase-designer：对话评测，状态是中文结论。"""
    version = data.get("skill_version", "")
    batch = data.get("batch_id", "")
    rows = []
    for i, r in enumerate(data.get("results") or []):
        runs = r.get("runs") or []
        status = AGENT_STATUS.get(r.get("判定", ""), "manual")
        methods = r.get("判定方式") or []
        first = runs[0] if runs else {}
        rows.append({
            "batch_id": batch,
            "source": "testcase-designer",
            "channel": "agent",
            "case_id": _case_id(r.get("id", ""), i),
            "title": r.get("用例标题", ""),
            "module": r.get("所属模块", ""),
            "priority": r.get("优先级", ""),
            "status": status,
            "decision_method": "、".join(methods) if methods else "未标注",
            "expected": r.get("预期结果", ""),
            "actual": (first.get("output") or "")[:200],
            "reason": first.get("reason", ""),
            "retried": (r.get("通过次数", 0) + r.get("失败次数", 0)) > 1,
            "evidence": _agent_evidence(r.get("证据"), path, i),
            "version": version,
            "target": data.get("target", ""),
            "env": "评测环境",
        })
    return rows


def load_rows(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    fmt = detect_format(data)
    if fmt == "ai-auto-test":
        rows = normalize_auto_test(data, path)
    elif fmt == "testcase-designer":
        rows = normalize_agent(data, path)
    else:
        raise ValueError(f"{path}: 无法识别的结果格式（既不是 ai-auto-test 也不是 testcase-designer 的结果）")
    # 源结果可能来自没开脱敏的旧版本或外部工具；汇总层必须在落盘前再过一遍。
    return fmt, [redact(r) for r in rows]


def gate_hits(summary, rules):
    """按显式配置的门禁条件判断是否放行。没配置就只提示，不替项目决定。"""
    hits = []
    if "blocked" in rules and summary["阻断"]:
        hits.append(f"阻断 {summary['阻断']} 条")
    if "manual" in rules and summary["待人工"]:
        hits.append(f"待人工 {summary['待人工']} 条")
    if "p0" in rules and summary["P0失败"]:
        hits.append(f"P0 失败/不稳定 {summary['P0失败']} 条")
    return hits


def summarize(rows):
    by_status = Counter(r["status"] for r in rows)
    by_source = Counter(r["source"] for r in rows)
    by_source_status = {}
    for r in rows:
        by_source_status.setdefault(r["source"], Counter())[r["status"]] += 1

    decided = sum(by_status[s] for s in DECIDED)
    total = len(rows)
    p0_bad = sum(1 for r in rows
                 if str(r.get("priority", "")).upper() == "P0"
                 and r["status"] in ("failed", "flaky"))
    return {
        "用例总数": total,
        "通过": by_status.get("passed", 0),
        "失败": by_status.get("failed", 0),
        "不稳定": by_status.get("flaky", 0),
        "阻断": by_status.get("blocked", 0),
        "跳过": by_status.get("skipped", 0),
        "待人工": by_status.get("manual", 0),
        "已判定通过率": round(by_status.get("passed", 0) / max(decided, 1), 3),
        "自动执行完成率": round(decided / max(total, 1), 3),
        "P0失败": p0_bad,
        "按来源": dict(by_source),
        "按来源状态": {k: dict(v) for k, v in by_source_status.items()},
    }


def diff_baseline(rows, path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        print(f"[警告] 基线读取失败，跳过对比：{e}")
        return None
    old_map = {f"{r.get('source')}|{r.get('case_id')}": r.get("status")
               for r in old.get("cases") or []}
    new_map = {f"{r['source']}|{r['case_id']}": r["status"] for r in rows}
    label = {f"{r['source']}|{r['case_id']}": f"[{r['source']}] {r['title']}" for r in rows}
    return {
        "新增失败": [label[k] for k, v in new_map.items()
                     if v in ("failed", "flaky") and old_map.get(k) == "passed"],
        "已修复": [label[k] for k, v in new_map.items()
                   if v == "passed" and old_map.get(k) in ("failed", "flaky")],
        "持续失败": [label[k] for k, v in new_map.items()
                     if v in ("failed", "flaky") and old_map.get(k) in ("failed", "flaky")],
        "新增用例": [label[k] for k in new_map if k not in old_map],
    }


def write_md(payload, path):
    s, rows, diff = payload["summary"], payload["cases"], payload.get("diff")
    lines = [
        "# 统一测试结果汇总",
        "",
        f"- 汇总时间：{payload['run_at']}",
        f"- 批次：`{payload['batch_id']}`",
        f"- 来源：{'、'.join(f'{k}（{v} 条）' for k, v in s['按来源'].items())}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 已判定通过率 | {s['已判定通过率'] * 100:.1f}% |",
        f"| 自动执行完成率 | {s['自动执行完成率'] * 100:.1f}% |",
        f"| 通过 / 失败 / 不稳定 / 阻断 / 跳过 / 待人工 | "
        f"{s['通过']} / {s['失败']} / {s['不稳定']} / {s['阻断']} / {s['跳过']} / {s['待人工']} |",
        f"| P0 失败（含不稳定） | {s['P0失败']} |",
        "",
        "口径：已判定通过率 = 通过 /（通过+失败+不稳定）；自动执行完成率 =（通过+失败+不稳定）/ 总数。",
        "阻断、跳过、待人工不进入分子，也不当作产品失败。",
        "",
    ]
    gate = payload.get("gate") or {}
    if gate:
        lines += ["## 门禁", ""]
        if not gate.get("configured"):
            lines.append(
                "- 未配置门禁策略：本次不判断是否允许发布。"
                f"阻断 {s['阻断']}、待人工 {s['待人工']}、P0 失败 {s['P0失败']} 已单列，请显式确认。")
        elif gate.get("hits"):
            lines.append("- **不通过**：" + "；".join(gate["hits"]))
        else:
            lines.append("- 通过：已配置条件（" + "、".join(gate["rules"]) + "）均未触发。")
        lines.append("")
    if diff:
        lines += ["## 与基线对比", ""]
        for k in ("新增失败", "已修复", "持续失败", "新增用例"):
            v = diff.get(k) or []
            lines.append(f"- **{k}**（{len(v)}）：" + ("、".join(v) if v else "无"))
        lines.append("")

    lines += ["## 明细", "",
              "| 来源 | ID | 模块 | 用例 | 优先级 | 结果 | 判定方式 | 说明 |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        why = (r.get("reason") or "").replace("|", "/")[:60]
        lines.append(f"| {r['source']} | {r['case_id']} | {r['module']} | {r['title']} | "
                     f"{r['priority']} | **{STATUS_LABEL.get(r['status'], r['status'])}** | "
                     f"{r['decision_method']} | {why} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_html(payload, path):
    import html as H
    s, rows, diff = payload["summary"], payload["cases"], payload.get("diff")
    color = {"passed": "#0f9d58", "failed": "#d93025", "flaky": "#e8a33d",
             "blocked": "#7257a8", "skipped": "#8c96a3", "manual": "#5b6b7f"}
    total = max(len(rows), 1)

    def bar(label, n, c):
        pct = n / total * 100
        return (f'<div class="bar-row"><span class="bar-label">{label}</span>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%;background:{c}"></div></div>'
                f'<span class="bar-num">{n}</span></div>')

    source_cards = "".join(
        f'<div class="kpi"><div class="n">{v}</div><div class="t">{H.escape(k)}</div></div>'
        for k, v in s["按来源"].items()
    )
    rows_html = "".join(
        f'<tr><td class="mono">{H.escape(r["source"])}</td><td class="mono">{H.escape(r["case_id"])}</td>'
        f'<td>{H.escape(r["module"])}</td><td><strong>{H.escape(r["title"])}</strong></td>'
        f'<td>{H.escape(str(r["priority"]))}</td>'
        f'<td><span class="pill" style="background:{color.get(r["status"], "#5b6b7f")}1a;'
        f'color:{color.get(r["status"], "#5b6b7f")};border-color:{color.get(r["status"], "#5b6b7f")}40">'
        f'{STATUS_LABEL.get(r["status"], r["status"])}</span></td>'
        f'<td class="mono">{H.escape(r["decision_method"])}</td>'
        f'<td class="why">{H.escape((r.get("reason") or "")[:120])}</td></tr>'
        for r in rows
    )
    diff_html = ""
    if diff:
        items = "".join(f'<li><b>{H.escape(k)}</b>（{len(v)}）：{H.escape("、".join(v)) or "无"}</li>'
                        for k, v in diff.items())
        diff_html = f'<section><h2>与基线对比</h2><ul class="diff">{items}</ul></section>'

    gate = payload.get("gate") or {}
    gate_html = ""
    if gate:
        if not gate.get("configured"):
            msg = ("未配置门禁策略 —— 本次不判断是否允许发布。"
                   f"阻断 {s['阻断']}、待人工 {s['待人工']}、P0 失败 {s['P0失败']} 已单列，请显式确认。")
            gate_html = f'<section><h2>门禁</h2><p class="why">{H.escape(msg)}</p></section>'
        elif gate.get("hits"):
            gate_html = ('<section><h2>门禁</h2><p class="why" style="color:#d93025">'
                         f'不通过：{H.escape("；".join(gate["hits"]))}</p></section>')
        else:
            gate_html = ('<section><h2>门禁</h2><p class="why">通过：已配置条件（'
                         f'{H.escape("、".join(gate["rules"]))}）均未触发。</p></section>')

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>统一测试结果汇总</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--text:#1c2430;--muted:#68727f}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:36px 24px 72px}}
h1{{font-size:24px;margin:0 0 6px;letter-spacing:-.2px}}
.sub{{color:var(--muted);font-size:13px}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0 10px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}}
.kpi .n{{font-size:26px;font-weight:650;letter-spacing:-1px}}
.kpi .t{{color:var(--muted);font-size:12px;margin-top:2px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin-top:18px}}
h2{{font-size:16px;margin:0 0 16px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th{{text-align:left;color:var(--muted);font-weight:500;font-size:12px;
border-bottom:1px solid var(--line);padding:8px 10px;white-space:nowrap}}
td{{border-bottom:1px solid #f0f2f4;padding:10px;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
.pill{{display:inline-block;font-size:12px;padding:1px 8px;border-radius:20px;border:1px solid;white-space:nowrap}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}}
.why{{color:var(--muted);font-size:12.5px;max-width:320px}}
.bar-row{{display:flex;align-items:center;gap:10px;margin:8px 0}}
.bar-label{{width:54px;color:var(--muted);font-size:13px}}
.bar-track{{flex:1;height:8px;background:#eef0f3;border-radius:6px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:6px}}
.bar-num{{width:26px;text-align:right;font-size:13px;font-variant-numeric:tabular-nums}}
ul.diff{{margin:0;padding-left:18px;font-size:13.5px}}
ul.diff li{{margin:5px 0}}
footer{{color:var(--muted);font-size:12px;margin-top:26px;text-align:center}}
</style></head><body><div class="wrap">
<header>
  <h1>统一测试结果汇总</h1>
  <div class="sub">{H.escape(payload['run_at'])}　·　批次 {H.escape(payload['batch_id'])}　·　
  共 {s['用例总数']} 条，来自 {len(s['按来源'])} 个执行通道</div>
</header>
<div class="kpis">
  <div class="kpi"><div class="n">{s['已判定通过率']*100:.0f}%</div><div class="t">已判定通过率</div></div>
  <div class="kpi"><div class="n">{s['自动执行完成率']*100:.0f}%</div><div class="t">自动执行完成率</div></div>
  <div class="kpi"><div class="n">{s['P0失败']}</div><div class="t">P0 失败 / 不稳定</div></div>
  <div class="kpi"><div class="n">{s['用例总数']}</div><div class="t">参与汇总用例</div></div>
</div>
<section><h2>按执行通道</h2><div class="kpis">{source_cards}</div></section>
<section><h2>结果分布</h2>
{bar("通过", s["通过"], color["passed"])}
{bar("失败", s["失败"], color["failed"])}
{bar("不稳定", s["不稳定"], color["flaky"])}
{bar("阻断", s["阻断"], color["blocked"])}
{bar("跳过", s["跳过"], color["skipped"])}
{bar("待人工", s["待人工"], color["manual"])}
</section>
{gate_html}
{diff_html}
<section><h2>用例明细</h2><table>
<thead><tr><th>来源</th><th>ID</th><th>模块</th><th>用例</th><th>优先级</th><th>结果</th><th>判定方式</th><th>说明</th></tr></thead>
<tbody>{rows_html}</tbody></table></section>
<footer>由 ai-auto-test / merge_results.py 生成，单文件离线可看</footer>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    ap = argparse.ArgumentParser(description="跨引擎测试结果汇总")
    ap.add_argument("results", nargs="+", help="若干 result.json（API/Web 与 Agent 均可）")
    ap.add_argument("--out", default="./汇总结果", help="输出目录")
    ap.add_argument("--baseline", help="上一轮合并结果，用于回归对比")
    ap.add_argument("--gate", default="",
                    help="门禁条件，逗号分隔：blocked,manual,p0。不配则只提示，不替项目决定放行")
    ap.add_argument("--no-redact", dest="redact", action="store_false",
                    help="关闭脱敏（仅本地调试；会把明文写进汇总结果）")
    ap.add_argument("--redact-keys", default="",
                    help="追加业务敏感字段名，逗号分隔，如 phone,id_card")
    args = ap.parse_args()

    configure_redaction(args.redact, [k.strip() for k in args.redact_keys.split(",")])
    if not args.redact:
        print("[警告] 已关闭脱敏：汇总结果将包含源文件中的明文，仅限本地调试。")

    rows, sources, errors = [], [], []
    for path in args.results:
        try:
            fmt, part = load_rows(path)
        except Exception as e:
            errors.append(f"{path}: {e}")
            continue
        sources.append(fmt)
        rows.extend(part)

    if errors:
        print("[FAIL] 以下结果文件不可用：")
        for e in errors:
            print("  -", e)
        return 2
    if not rows:
        print("[FAIL] 没有可汇总的用例")
        return 2

    summary = summarize(rows)
    rules = [r.strip().lower() for r in args.gate.split(",") if r.strip()]
    gate = {
        "configured": bool(rules),
        "rules": rules,
        "hits": gate_hits(summary, rules),
    }
    batch_id = datetime.now().strftime("%Y%m%d%H%M%S")
    payload = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "merger_version": VERSION,
        "batch_id": batch_id,
        "sources": sources,
        "summary": summary,
        "gate": gate,
        "cases": rows,
        "diff": diff_baseline(rows, args.baseline) if args.baseline else None,
    }

    os.makedirs(args.out, exist_ok=True)
    json_path = os.path.join(args.out, "merged_result.json")
    md_path = os.path.join(args.out, "汇总报告.md")
    html_path = os.path.join(args.out, "汇总报告.html")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    write_md(payload, md_path)
    write_html(payload, html_path)

    print("=" * 54)
    print(f"汇总 {summary['用例总数']} 条｜来源 " +
          "、".join(f"{k}:{v}" for k, v in summary["按来源"].items()))
    print(f"通过 {summary['通过']}｜失败 {summary['失败']}｜不稳定 {summary['不稳定']}"
          f"｜阻断 {summary['阻断']}｜跳过 {summary['跳过']}｜待人工 {summary['待人工']}")
    print(f"已判定通过率 {summary['已判定通过率']*100:.1f}%　"
          f"自动执行完成率 {summary['自动执行完成率']*100:.1f}%　P0 失败 {summary['P0失败']}")
    if not gate["configured"]:
        print(f"[提示] 未配置门禁策略：不判断是否允许发布。"
              f"阻断 {summary['阻断']}、待人工 {summary['待人工']}、P0 失败 {summary['P0失败']} 已单列。")
    elif gate["hits"]:
        print("[门禁] 不通过：" + "；".join(gate["hits"]))
    else:
        print("[门禁] 通过：已配置条件均未触发。")
    if payload["diff"]:
        for k, v in payload["diff"].items():
            print(f"{k}：{len(v)} 条")
    print(f"\n报告：{html_path}")
    print(f"数据：{json_path}")
    print("=" * 54)

    if gate["hits"]:
        return 1
    return 1 if (summary["失败"] or summary["不稳定"]) else 0


if __name__ == "__main__":
    sys.exit(main())
