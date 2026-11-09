#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档一致性校验 —— 让「契约性事实」没法悄悄漂移。

为什么需要它：
    断言类型、状态集、认证方式、核心模块、字段列表这类事实，代码里有一份真值，
    文档里还有一到两份副本。副本没有编译期检查，只能靠人记得同步，
    结果是已经漂移过多次（SKILL.md 里「两条/三条」「九种/13 类」「四种/五种」「六个/9 个」）。

    本脚本从代码读出真值，再扫文档里声明的数字与清单，不一致直接非零退出。
    它不检查文风，只检查「能被代码证伪的事实」。

检查项：
    1. 断言类型集     core/asserts.py 的 SUPPORTED  vs references/spec-schema.md 的小节集合
    2. 断言类型数     SUPPORTED 数量                     vs SKILL.md「共 N 类」
    3. 状态集         core/runner.py 的 STATUS_LABEL    vs references/result-and-triage.md
    4. 状态集         STATUS_LABEL 数量                  vs SKILL.md「N 种状态不许合并」那一行
    5. 认证方式集     core/auth.py 的 AUTH_TYPES        vs references/auth-adapters.md 的小节集合
    6. 认证方式数     AUTH_TYPES 数量                    vs SKILL.md「N 种类型」
    7. 核心模块       core/*.py 文件名                   vs SKILL.md「N 个模块」且逐个被点名
    8. 硬规矩条数     SKILL.md 声明                      vs 实际编号条目数
    9. 版本号         core/runner.py 的 VERSION          vs CHANGELOG.md 顶部最新条目
   10. 判定类型       同目录 testcase-designer 的 kind == vs testcase-designer/SKILL.md「只有 N 种」
   11. 用例字段数     render_cases.py 的 FIELDS          vs testcase-designer/SKILL.md「10 字段」

用法：
    python3 check_docs.py
    python3 check_docs.py --json          # 机器可读，便于挂 CI

退出码：
    0 = 全部一致
    1 = 发现漂移
    2 = 环境问题（文件缺失 / 模块导入失败）
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SELF = SCRIPTS.parent                      # ai-auto-test/
SIBLING = SELF.parent / "testcase-designer"

CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn2int(text):
    """把「十三」「五」「9」这类写法转成整数，转不了返回 None。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN.get(s[1:2], 0)
    if "十" in s:
        head, _, tail = s.partition("十")
        return CN.get(head, 1) * 10 + (CN.get(tail[:1], 0) if tail else 0)
    return CN.get(s)


def read(path):
    return Path(path).read_text(encoding="utf-8")


def get_section(text, heading):
    """取某个 ## 章节的正文（到下一个 ## 为止）。"""
    out, on = [], False
    for line in text.splitlines():
        if on and re.match(r"^##\s+", line):
            break
        if on:
            out.append(line)
        elif line.strip() == heading.strip():
            on = True
    return "\n".join(out)


def row(scope, item, ok, truth, found, detail=""):
    return {"scope": scope, "item": item, "ok": ok,
            "truth": truth, "found": found, "detail": detail}


def cmp_set(scope, item, truth, found):
    """集合一致性：缺了、多了都要报。"""
    t, f = set(truth), set(found)
    if t == f:
        return row(scope, item, True, len(t), len(f))
    parts = []
    if t - f:
        parts.append("文档缺 " + "、".join(sorted(t - f)))
    if f - t:
        parts.append("文档多 " + "、".join(sorted(f - t)))
    return row(scope, item, False, len(t), len(f), "；".join(parts))


def cmp_num(scope, item, truth, found, hint=""):
    if found is None:
        return row(scope, item, False, truth, "没找到声明", hint or "声明被删掉或改了写法")
    if truth == found:
        return row(scope, item, True, truth, found)
    return row(scope, item, False, truth, found, "数字不一致")


def code_facts():
    sys.path.insert(0, str(SCRIPTS))
    from core.asserts import SUPPORTED
    from core.auth import AUTH_TYPES
    from core.runner import STATUS_LABEL, VERSION

    modules = sorted(p.stem for p in (SCRIPTS / "core").glob("*.py")
                     if p.stem != "__init__")
    return {
        "断言类型": list(SUPPORTED.keys()),
        "状态": list(STATUS_LABEL.keys()),
        "认证方式": list(AUTH_TYPES),
        "核心模块": modules,
        "版本": VERSION,
    }


def check_self():
    facts = code_facts()
    skill_md = read(SELF / "SKILL.md")
    spec_schema = read(SELF / "references" / "spec-schema.md")
    triage = read(SELF / "references" / "result-and-triage.md")
    auth_doc = read(SELF / "references" / "auth-adapters.md")
    out = []

    # 1. 断言类型集：spec-schema 的 ### 小节应恰好覆盖全部类型
    region = get_section(spec_schema, "## 断言类型全表")
    heads = re.findall(r"^###\s+(.+)$", region, re.M)
    names = []
    for h in heads:
        names += [p.strip() for p in re.split(r"\s*/\s*", h.strip())]
    out.append(cmp_set("ai-auto-test", "断言类型集 · spec-schema 小节",
                       facts["断言类型"], names))

    # 2. 断言类型数：SKILL.md「共 N 类」
    m = re.search(r"共\s*(\d+)\s*类", skill_md)
    out.append(cmp_num("ai-auto-test", "断言类型数 · SKILL.md 声明",
                       len(facts["断言类型"]), int(m.group(1)) if m else None,
                       "断言表被删了"))

    # 3. 状态集：triage 的标题数字 + 五个状态名都在
    m = re.search(r"##\s*([一二三四五六七八九十\d]+)\s*种状态", triage)
    declared = cn2int(m.group(1)) if m else None
    out.append(cmp_num("ai-auto-test", "状态集 · result-and-triage 标题",
                       len(facts["状态"]), declared))
    out.append(cmp_set("ai-auto-test", "状态集 · triage 正文出现",
                       facts["状态"], [s for s in facts["状态"] if s in triage]))

    # 4. 状态集：SKILL.md 硬性规则那一行
    line = next((l for l in skill_md.splitlines() if "种状态不许合并" in l), "")
    m = re.search(r"([一二三四五六七八九十\d]+)\s*种状态不许合并", line)
    declared = cn2int(m.group(1)) if m else None
    out.append(cmp_num("ai-auto-test", "状态集 · SKILL.md 硬性规则",
                       len(facts["状态"]), declared))
    absent = [s for s in facts["状态"] if s not in line]
    out.append(row("ai-auto-test", "状态名 · SKILL.md 逐个点名",
                   not absent, len(facts["状态"]),
                   len(facts["状态"]) - len(absent),
                   "漏了 " + "、".join(absent) if absent else ""))

    # 5. 认证方式集：auth-adapters 的 ## 小节
    heads = re.findall(r"^##\s+(\S+)\s*$", auth_doc, re.M)
    out.append(cmp_set("ai-auto-test", "认证方式集 · auth-adapters 小节",
                       facts["认证方式"], [h for h in heads if h in facts["认证方式"]]))

    # 6. 认证方式数：SKILL.md「N 种类型」
    m = re.search(r"([一二三四五六七八九十\d]+)\s*种类型", skill_md)
    out.append(cmp_num("ai-auto-test", "认证方式数 · SKILL.md 声明",
                       len(facts["认证方式"]), cn2int(m.group(1)) if m else None))

    # 7. 核心模块：数字 + 每个模块被点名
    m = re.search(r"(\d+)\s*个模块", skill_md)
    out.append(cmp_num("ai-auto-test", "核心模块数 · SKILL.md 声明",
                       len(facts["核心模块"]), int(m.group(1)) if m else None))
    absent = [m for m in facts["核心模块"] if ("`%s`" % m) not in skill_md]
    out.append(row("ai-auto-test", "核心模块名 · SKILL.md 逐个点名",
                   not absent, len(facts["核心模块"]),
                   len(facts["核心模块"]) - len(absent),
                   "没提到 " + "、".join(absent) if absent else ""))

    # 8. 硬规矩条数：标题声明的数字 vs 实际编号条目
    m = re.search(r"\*\*([一二三四五六七八九十\d]+)条硬规矩[：:]\*\*", skill_md)
    if m:
        declared = cn2int(m.group(1))
        after = skill_md.split(m.group(0), 1)[1]
        items = 0
        for ln in after.splitlines():
            if re.match(r"^\d+\.\s", ln):
                items += 1
            elif ln.strip() and items:
                break
        out.append(cmp_num("ai-auto-test", "硬规矩条数 · SKILL.md 自述",
                           items, declared))
    else:
        out.append(row("ai-auto-test", "硬规矩条数 · SKILL.md 自述", True, 0, 0,
                       "没有这类声明，跳过"))

    # 9. 版本号：CHANGELOG 顶部最新一条 vs runner.VERSION
    # 这条容易被忽略：发版时改了代码忘了记 CHANGELOG（或反过来），
    # 结果 result.json 里的版本号和变更记录对不上，回溯时不知道跑的是哪一版。
    changelog = read(SELF / "CHANGELOG.md")
    m = re.search(r"^##\s+v(\d+\.\d+\.\d+)", changelog, re.M)
    out.append(cmp_num("ai-auto-test", "版本号 · CHANGELOG 最新条目",
                       facts["版本"], m.group(1) if m else None,
                       "CHANGELOG 顶部应有 ## vX.Y.Z 形式的条目"))
    return out


def check_sibling():
    if not (SIBLING / "SKILL.md").exists():
        return [row("testcase-designer", "整体", True, 0, 0, "未安装，跳过")]
    out = []
    skill_md = read(SIBLING / "SKILL.md")

    # 9. 判定类型：run_agent_eval.py 的 kind == 分支
    src = read(SIBLING / "scripts" / "run_agent_eval.py")
    judges, seen = [], set()
    for k in re.findall(r'if kind == "([^"]+)"', src):
        if k not in seen:
            seen.add(k)
            judges.append(k)
    line = next((l for l in skill_md.splitlines() if "判定类型只有" in l), "")
    m = re.search(r"判定类型只有([一二三四五六七八九十\d]+)种", line)
    out.append(cmp_num("testcase-designer", "判定类型数 · SKILL.md 声明",
                       len(judges), cn2int(m.group(1)) if m else None))
    absent = [j for j in judges if j not in line]
    out.append(row("testcase-designer", "判定类型名 · SKILL.md 逐个点名",
                   not absent, len(judges), len(judges) - len(absent),
                   "漏了 " + "、".join(absent) if absent else ""))

    # 10. 用例字段数：render_cases.py 的 FIELDS
    fsrc = read(SIBLING / "scripts" / "render_cases.py")
    m = re.search(r"FIELDS\s*=\s*\[(.*?)\]", fsrc, re.S)
    fields = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    declared = 10 if re.search(r"10\s*个?字段", skill_md) else None
    out.append(cmp_num("testcase-designer", "用例字段数 · SKILL.md 声明",
                       len(fields), declared))
    return out


def main():
    ap = argparse.ArgumentParser(description="文档与代码的一致性校验")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    try:
        rows = check_self() + check_sibling()
    except ImportError as e:
        print("导入核心模块失败：%s" % e, file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print("找不到文件：%s" % e, file=sys.stderr)
        return 2

    bad = [r for r in rows if not r["ok"]]

    if args.json:
        print(json.dumps({"ok": not bad, "checks": rows}, ensure_ascii=False, indent=2))
    else:
        scope = None
        for r in rows:
            if r["scope"] != scope:
                scope = r["scope"]
                print("\n%s" % scope)
            flag = "[OK]  " if r["ok"] else "[漂移]"
            line = "  %s %-34s 代码 %-3s / 文档 %-3s" % (
                flag, r["item"], r["truth"], r["found"])
            if r["detail"]:
                line += "  <- " + r["detail"]
            print(line)
        print()
        if bad:
            print("结论：%d 处漂移，%d 项通过。文档与代码不一致，先修文档再交付。"
                  % (len(bad), len(rows) - len(bad)))
        else:
            print("结论：%d 项全部一致。" % len(rows))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
