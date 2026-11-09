# -*- coding: utf-8 -*-
"""需求覆盖 —— 把「跑了哪些用例」聚合成「验证了哪些需求」。

为什么单独一个模块：
    覆盖度是**口径**问题，不是渲染问题。跑完才能算，算错了不会抛异常，
    只会让报告看起来更漂亮 —— 这正是最需要能单独测的那类代码。

三个刻意的取舍：

  1. 分母只认规格顶层 `需求`。规格里没声明需求清单时，分母退化成「用例里出现过的
     需求点」，并在结果里标 `declared_source: "observed"`。这时「覆盖率 100%」
     只说明「标了需求点的都覆盖了」，**不说明需求清单是完整的** —— 报告必须把这句话
     写出来，否则就成了新的自欺欺人。

  2. 一条需求的状态取它所有关联用例里**最差**的那个。一条需求下有 3 条用例、2 过 1 挂，
     这条需求就是 failed。按平均或按最好算，会让漏测看起来像达标。

  3. `--only` 或 `--fail-fast` 中断时标 `partial_run`。筛选执行算出来的覆盖度不是
     覆盖度，是「筛选范围内的覆盖度」，不能拿去汇报。

状态优先级（越大越好）：
    failed < blocked < skipped < manual < passed
"""

STATUS_RANK = {"failed": 0, "blocked": 1, "skipped": 2, "manual": 3, "passed": 4}

# 需求状态里，`uncovered` 不是用例状态，是「一条用例都没关联」，单独放在最低位。
UNCOVERED = "uncovered"


def _as_list(value):
    """允许写成单值，省得每条只有一个需求点的用例都要套一层数组。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def normalize_requirements(spec):
    """规格顶层 `需求` → [{"id", "说明"}]。

    两种写法都收：`["R-01", "R-02"]` 与 `[{"id": "R-01", "说明": "..."}]`。
    混排也认，方便从需求文档里直接粘 ID 列表再逐步补说明。
    """
    out, seen = [], set()
    for item in _as_list((spec or {}).get("需求")):
        if isinstance(item, dict):
            rid = str(item.get("id") or item.get("需求点") or "").strip()
            note = str(item.get("说明") or item.get("标题") or item.get("name") or "").strip()
        else:
            rid, note = str(item).strip(), ""
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "说明": note})
    return out


def case_requirements(case):
    """用例的 `需求点` → 去重后的字符串列表（保持书写顺序）。"""
    out, seen = [], set()
    for item in _as_list((case or {}).get("需求点")):
        rid = str(item).strip()
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def validate_requirements_shape(value, where):
    """`需求点` 的形状检查。编译期与 lint 共用一份，避免两处判断不一致。"""
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return [("error", "%s 的「需求点」是空字符串 —— 要么写具体 ID，要么删掉这个键" % where)]
        return []
    if not isinstance(value, (list, tuple)):
        return [("error", "%s 的「需求点」必须是字符串或字符串数组" % where)]
    problems = []
    if not value:
        problems.append(("warning", "%s 的「需求点」是空数组，等于没标，建议直接删掉" % where))
    for i, x in enumerate(value, 1):
        if not isinstance(x, str) or not x.strip():
            problems.append(("error", "%s 的「需求点」第 %d 项不是非空字符串" % (where, i)))
    return problems


def check_against_declared(case, declared_ids, where):
    """需求点引用了规格没声明的 ID —— 拼错一个字母，覆盖度就悄悄少一个，必须拦。"""
    if not declared_ids:
        return []
    unknown = [r for r in case_requirements(case) if r not in declared_ids]
    if not unknown:
        return []
    return [("error", "%s 的「需求点」引用了规格「需求」里没有的 ID：%s"
             "（拼错的 ID 会让覆盖度静默变低）" % (where, "、".join(unknown)))]


def build_coverage(spec, results, selected="", interrupted=False):
    """跑完之后算覆盖度。results 里的每条用例需带 `requirements` 字段。"""
    declared = normalize_requirements(spec)
    declared_ids = [d["id"] for d in declared]
    notes = {d["id"]: d["说明"] for d in declared}

    by_req, untracked = {}, []
    tracked_cases = 0
    for r in results or []:
        reqs = list(r.get("requirements") or [])
        if not reqs:
            untracked.append(r.get("id", ""))
            continue
        tracked_cases += 1
        for rid in reqs:
            by_req.setdefault(rid, []).append(r)

    # 声明在前、观测到的补在后：报告里顺序稳定，读的人不用每次重新找
    ids = declared_ids + [rid for rid in by_req if rid not in set(declared_ids)]

    items = []
    for rid in ids:
        rows = by_req.get(rid, [])
        if not rows:
            status = UNCOVERED
        else:
            status = min((r.get("status") or "failed" for r in rows),
                         key=lambda s: STATUS_RANK.get(s, -1))
        items.append({
            "id": rid,
            "note": notes.get(rid, ""),
            "declared": rid in set(declared_ids),
            "status": status,
            "cases": [r.get("id", "") for r in rows],
            "auto_cases": len([r for r in rows if r.get("status") != "manual"]),
        })

    def pick(status):
        return [i["id"] for i in items if i["status"] == status]

    verified = pick("passed")
    uncovered = pick(UNCOVERED)
    if declared:
        source = "spec"
    elif items:
        source = "observed"
    else:
        source = "none"

    return {
        # enabled=False 时报告整段不渲染：老规格不写需求点，不该多出一堆空表
        "enabled": bool(items),
        "declared_source": source,
        "total": len(items),
        "covered": len(items) - len(uncovered),
        "verified": len(verified),
        "rate": round(len(verified) * 100.0 / len(items), 1) if items else 0.0,
        "verified_ids": verified,
        "uncovered": uncovered,
        "failed": pick("failed"),
        "blocked": pick("blocked"),
        "skipped": pick("skipped"),
        "manual": pick("manual"),
        "items": items,
        "tracked_cases": tracked_cases,
        "untracked_cases": untracked,
        # 筛选执行 / 提前中断算出来的不是覆盖度，是范围内覆盖度
        "partial_run": bool(selected or interrupted),
        "filter": selected or "",
        "interrupted": bool(interrupted),
    }


def summarize(coverage):
    """一句话口径，md 报告与命令行输出共用，避免两处措辞不一致。"""
    if not coverage or not coverage.get("enabled"):
        return ""
    text = "需求覆盖 %d/%d（已验证 %d）" % (
        coverage["covered"], coverage["total"], coverage["verified"])
    if coverage["uncovered"]:
        text += "，%d 个需求点无用例" % len(coverage["uncovered"])
    if coverage["failed"]:
        text += "，%d 个需求点有失败" % len(coverage["failed"])
    return text
