"""断言 DSL —— 把规格里的断言对象判定为 通过/不通过 + 可读说明。

设计原则：
  1. 一条断言只判一件事，说明文字必须能直接贴进缺陷单（含实际值与期望值）。
  2. 断言值支持 ${变量} 插值，可跨步骤比对（如「第 2 页首条 ID 不等于第 1 页首条」）。
  3. 未知断言类型一律判失败并显式报错 —— 绝不静默通过。
"""

import json
import os
import re

# 断言类型 × 判定方式，供 lint 与文档共用
SUPPORTED = {
    "状态码": ["等于", "不等于", "在"],
    "耗时": ["小于"],
    "业务状态": ["等于", "不等于"],
    "成功标志": ["等于"],
    "包含": ["值"],
    "不包含": ["值"],
    "JSON字段": [
        "路径",
        "等于",
        "不等于",
        "正则",
        "长度",
        "大于",
        "大于等于",
        "小于等于",
        "期望在",
        "包含",
        "不包含",
        "非空",
        "为空",
        "是整数",
        "是数组",
        "是布尔",
        "是字符串",
    ],
    "路径存在": ["路径"],
    "下载": ["存在", "后缀", "名称包含", "最小字节", "最大字节", "数量"],
    "响应头": ["名称", "包含", "正则", "等于"],
    "响应契约": ["schema", "来源", "operationId", "响应", "状态码", "内容类型"],
    "并发结果": ["请求数", "成功数", "失败数", "状态码", "JSON字段"],
    "DB": ["来源", "数量", "字段"],
    "MQ": ["来源", "数量", "字段", "事件类型"],
    "Trace": ["来源", "span", "错误数", "trace_id"],
    # ---- 以下四类只对 UI 步骤生效（响应里带 ui=True）
    "页面": ["选择器", "存在", "可见", "数量", "文本", "文本等于", "不含文本", "值", "属性", "可用"],
    "URL": ["等于", "包含", "不含", "正则"],
    "控制台": ["无错误", "不含"],
    "网络": ["无失败请求", "允许", "请求", "方法", "次数", "请求体包含"],
}

# 出现在「预期结果」里就说明这条用例不可判定的措辞
VAGUE_WORDS = ["正常", "正确", "符合预期", "无异常", "没问题", "ok", "成功即可"]


def find_vague(text):
    """预期结果里的不可判定措辞。

    英文词按词边界匹配：早年用 `"ok" in text` 判，结果「返回 data.token 非空」
    被当成含糊表述拦下来 —— token 里含 ok。中文词仍按子串匹配，
    中文没有词边界，按子串反而更准。
    """
    hits = []
    for w in VAGUE_WORDS:
        if w.isascii():
            if re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(w), text or "", re.I):
                hits.append(w)
        elif w in (text or ""):
            hits.append(w)
    return hits


class AssertError(Exception):
    """规格写错了（不是被测系统的缺陷）。"""


def deep_get(obj, path):
    """支持 a.b.0.c 形式的取值；任一层缺失返回 None。"""
    if path is None or path == "":
        return obj
    cur = obj
    for seg in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


_VAR_RE = re.compile(r"\$\{([\w.\-]+)\}")


def fill(value, variables, keep_types=False):
    """把 ${var} / ${env.KEY} 替换成变量值；非字符串原样返回。

    `keep_types=True` 时，**整个字符串恰好就是一个变量**（`"${age}"`）保留原类型：
    数字还是数字、布尔还是布尔、对象还是对象。

    为什么需要它：数据驱动的数据组大多用来喂请求体，统一转成字符串会把 18 变成 "18"，
    服务端要么类型校验报错、要么按字符串比较（"18" != 18），测出来的行为跟真实调用
    不一样。默认关闭 —— URL、头部、文件路径这些拼接场景仍然该走字符串。
    """
    if isinstance(value, list):
        return [fill(v, variables, keep_types) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, variables, keep_types) for k, v in value.items()}
    if not isinstance(value, str):
        return value

    if keep_types:
        m = _VAR_RE.fullmatch(value)
        if m and m.group(1) in variables:
            return variables[m.group(1)]

    def rep(m):
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        return m.group(0)

    return _VAR_RE.sub(rep, value)


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _req(assertion, key, kind):
    """取必需键；缺失时抛 AssertError 而不是 KeyError。

    为什么较真：runner 只 catch AssertError，KeyError 会一路冒到最外层，
    用户看到的是一屏 traceback 而不是「规格错误：…」。规格写错是最常见的
    情况，必须让报错指向具体缺了哪个键。
    """
    if key not in assertion:
        raise AssertError("「%s」断言缺少「%s」：%r" % (kind, key, assertion))
    return assertion[key]


def _req_num(assertion, key, kind, variables=None):
    """取必需键并转成数值；转不了同样算规格错误。"""
    v = _to_num(fill(_req(assertion, key, kind), variables or {}))
    if v is None:
        raise AssertError("「%s」断言的「%s」必须是数字，实际是 %r"
                          % (kind, key, assertion[key]))
    return v


def _short(v, limit=120):
    try:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _num_eq(got, want):
    """容忍 0 与 "0"、1 与 1.0、13 与 "${total}" 填出来的 "13" 这类跨类型比较。"""
    if got == want:
        return True
    try:
        return float(got) == float(want)
    except (TypeError, ValueError):
        return False


def _to_num(v):
    """把字面量或 ${变量} 填出来的字符串转成数值；转不了返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_num(v, raw=None):
    """报告里显示填值后的数字，而不是 ${变量} 原文。"""
    if v is None:
        return "无法解析(%r)" % (raw,)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


_COUNT_OPS = (
    ("等于", lambda a, b: a == b),
    ("不等于", lambda a, b: a != b),
    ("大于等于", lambda a, b: a >= b),
    ("大于", lambda a, b: a > b),
    ("小于等于", lambda a, b: a <= b),
    ("小于", lambda a, b: a < b),
)


def judge_count(spec, n, variables, what="次数"):
    """判定「次数」这类计数条件：可以写整数（等于），也可以写 {大于等于: 1}。

    返回 (ok, 说明)。
    """
    if isinstance(spec, dict):
        for op, fn in _COUNT_OPS:
            if op in spec:
                want = _to_num(fill(spec[op], variables))
                if want is None:
                    raise AssertError("「%s」里的「%s」必须是数字，实际是 %r"
                                      % (what, op, spec[op]))
                return fn(n, want), "期望%s %s" % (op, _fmt_num(want, spec[op]))
        raise AssertError("「%s」的判定条件只支持 %s，实际是 %r"
                          % (what, "/".join(o for o, _ in _COUNT_OPS), spec))
    want = _to_num(fill(spec, variables))
    if want is None:
        raise AssertError("「%s」必须是整数或 %s 之类的条件，实际是 %r"
                          % (what, "{大于等于: 1}", spec))
    return n == want, "期望 %s" % _fmt_num(want, spec)


def _judge_network_requests(assertion, page_json, variables, target):
    """按 URL 片段匹配本步骤发出的请求，判调用次数 / 请求体内容。

    为什么按「本步骤期间」统计：同一条「提交订单只调用 1 次」如果按会话累积算，
    前面步骤调过同一个接口就会变成 2，报出来的缺陷是假的。执行器在动作前打点，
    这里读的就是打点之后的增量。
    """
    want = str(fill(target, variables))
    method = str(fill(assertion.get("方法") or "", variables)).upper()
    reqs = page_json.get("network_requests")
    if reqs is None:
        raise AssertError(
            "「网络」断言取不到请求明细（响应里没有 network_requests）。"
            "这条断言只能挂在浏览器步骤上，且需要重跑该步骤才会采到明细。"
        )
    if page_json.get("network_truncated"):
        raise AssertError(
            "本步骤的网络请求记录被截断（已达 ui.network_limit 上限），无法准确判定「%s」。"
            "与其给一个可能不准的结论，这里拒绝判定 —— 把规格里的 ui.network_limit "
            "调大后重跑。" % want
        )
    hits = [r for r in reqs
            if want in (r.get("url") or "")
            and (not method or (r.get("method") or "").upper() == method)]
    label = "%s%s" % (method + " " if method else "", want)
    if not hits and "次数" not in assertion:
        return False, "本步骤没有匹配「%s」的请求" % label

    if "次数" in assertion:
        ok, how = judge_count(assertion["次数"], len(hits), variables)
        return ok, "本步骤「%s」调用 %d 次（%s）" % (label, len(hits), how)

    if "请求体包含" in assertion:
        needles = [str(fill(v, variables)) for v in _as_list(assertion["请求体包含"])]
        bodies = "\n".join(r.get("body") or "" for r in hits)
        miss = [x for x in needles if x not in bodies]
        if not miss:
            return True, "「%s」请求体命中 %d 项（本步骤 %d 次请求）" % (
                label, len(needles), len(hits))
        return False, "「%s」请求体缺少 %s（实际：%s）" % (
            label, _short(miss, 60), _short(bodies, 200))

    raise AssertError(
        "「网络」断言给了「请求」但没给判定条件，需补「次数」或「请求体包含」：%r" % (assertion,)
    )


def judge(assertion, resp, variables):
    """判定单条断言。

    resp: {"status", "elapsed_ms", "json", "text", "headers"}
    返回 (ok: bool, detail: str)
    """
    if not isinstance(assertion, dict):
        raise AssertError("断言必须是一个对象，实际是 %r" % (assertion,))
    kind = assertion.get("类型")
    if not kind:
        raise AssertError("断言缺少「类型」字段：%r" % (assertion,))
    if kind not in SUPPORTED:
        raise AssertError("未知断言类型「%s」，可用：%s" % (kind, "、".join(SUPPORTED)))

    body_json = resp.get("json")
    body_text = resp.get("text") or ""
    got = None
    if "路径" in assertion:
        got = deep_get(body_json, assertion["路径"]) if body_json is not None else None

    # ---------------- HTTP 层
    if kind == "状态码":
        if resp.get("status") is None:
            # UI 步骤只有「导航」时才产生 HTTP 状态码。以前没导航也返回 200，
            # 于是挂在点击步骤上的「状态码 200」恒为真 —— 断言写了等于没写。
            # 这里宁可报规格错误，也不让它静默通过。
            raise AssertError(
                "这一步没有 HTTP 状态码可断言（UI 步骤且未发生页面导航）。"
                "要确认点击/提交的结果，请用「页面」「URL」「网络」断言；"
                "要拿接口状态码，请把该请求单独写成 HTTP 步骤。"
            )
        if "等于" in assertion:
            want = fill(assertion["等于"], variables, keep_types=True)
            ok = _num_eq(resp["status"], want)
            return ok, "HTTP %s（期望 %s）" % (resp["status"], want)
        if "不等于" in assertion:
            want = fill(assertion["不等于"], variables, keep_types=True)
            ok = not _num_eq(resp["status"], want)
            return ok, "HTTP %s（不应为 %s）" % (resp["status"], want)
        if "在" in assertion:
            want = [fill(v, variables, keep_types=True) for v in _as_list(assertion["在"])]
            ok = any(_num_eq(resp["status"], w) for w in want)
            return ok, "HTTP %s（期望属于 %s）" % (resp["status"], want)

    if kind == "耗时":
        limit = _req_num(assertion, "小于", kind, variables)
        ok = resp["elapsed_ms"] < limit
        return ok, "%sms（阈值 <%sms）" % (resp["elapsed_ms"], limit)

    if kind == "响应头":
        name = _req(assertion, "名称", kind)
        got_h = None
        for k, v in (resp.get("headers") or {}).items():
            if k.lower() == name.lower():
                got_h = v
                break
        if "等于" in assertion:
            want = fill(assertion["等于"], variables)
            ok = (got_h or "") == want
            return ok, "响应头 %s=%s（期望 %s）" % (name, got_h, want)
        if "包含" in assertion:
            want = fill(assertion["包含"], variables)
            ok = want in (got_h or "")
            return ok, "响应头 %s=%s（期望含 %s）" % (name, got_h, want)
        if "正则" in assertion:
            ok = got_h is not None and re.search(assertion["正则"], got_h) is not None
            return ok, "响应头 %s=%s（正则 %s）" % (name, got_h, assertion["正则"])
        raise AssertError("响应头断言需指定 等于/包含/正则")

    # ---------------- 业务层
    if kind == "业务状态":
        got_status = None if body_json is None else body_json.get("status")
        if "等于" in assertion:
            want = fill(assertion["等于"], variables, keep_types=True)
            ok = _num_eq(got_status, want)
            return ok, "body.status=%r（期望 %r）" % (got_status, want)
        if "不等于" in assertion:
            want = fill(assertion["不等于"], variables, keep_types=True)
            ok = not _num_eq(got_status, want)
            return ok, "body.status=%r（不应为 %r）" % (got_status, want)

    if kind == "成功标志":
        # keep_types：数据组里写 true 就是布尔 True，转成 "true" 之后 is 比较必假
        want = fill(assertion.get("等于", True), variables, keep_types=True)
        got_s = None if body_json is None else body_json.get("success")
        ok = got_s is want
        return ok, "body.success=%r（期望 %r）" % (got_s, want)

    if kind in ("包含", "不包含"):
        needles = [str(fill(v, variables)) for v in _as_list(_req(assertion, "值", kind))]
        found = [n for n in needles if n in body_text]
        if kind == "包含":
            ok = len(found) == len(needles)
            miss = [n for n in needles if n not in found]
            return ok, "全部命中" if ok else "响应缺少 %s" % _short(miss)
        ok = not found
        return ok, "未出现" if ok else "出现禁用内容 %s" % _short(found)

    # ---------------- 并发与只读观测层
    if kind == "并发结果":
        group = resp.get("concurrency")
        if not group:
            raise AssertError("「并发结果」只能挂在带「并发」配置的 HTTP 步骤上")
        rows = group.get("responses") or []
        if "请求数" in assertion:
            want = int(fill(assertion["请求数"], variables, keep_types=True))
            if len(rows) != want:
                return False, "并发完成 %d 请求（期望 %d）" % (len(rows), want)
        statuses = [r.get("status") for r in rows]
        if "成功数" in assertion:
            n = sum(1 for x in statuses if isinstance(x, int) and 200 <= x < 400)
            want = int(fill(assertion["成功数"], variables, keep_types=True))
            if n != want: return False, "并发 HTTP 成功 %d（期望 %d）" % (n, want)
        if "失败数" in assertion:
            n = sum(1 for x in statuses if not (isinstance(x, int) and 200 <= x < 400))
            want = int(fill(assertion["失败数"], variables, keep_types=True))
            if n != want: return False, "并发 HTTP 失败 %d（期望 %d）" % (n, want)
        rule = assertion.get("状态码") or {}
        if "全部在" in rule:
            allowed = [int(x) for x in _as_list(rule["全部在"])]
            bad = [x for x in statuses if x not in allowed]
            if bad: return False, "并发状态码 %s，期望全部属于 %s" % (statuses, allowed)
        if "计数" in rule:
            actual = {}
            for x in statuses: actual[str(x)] = actual.get(str(x), 0) + 1
            want = {str(k): int(v) for k, v in rule["计数"].items()}
            if actual != want: return False, "并发状态码分布 %s（期望 %s）" % (actual, want)
        field = assertion.get("JSON字段") or {}
        if field:
            path = _req(field, "路径", "并发结果.JSON字段")
            values = [deep_get(r.get("json"), path) for r in rows]
            if field.get("非空") and any(v in (None, "", [], {}) for v in values):
                return False, "并发响应字段 %s 存在空值" % path
            if "去重数" in field:
                want = int(fill(field["去重数"], variables, keep_types=True))
                got = len(set(json.dumps(v, ensure_ascii=False, sort_keys=True) for v in values))
                if got != want: return False, "并发字段 %s 去重数=%d（期望 %d）" % (path, got, want)
        return True, "并发 %d 请求符合聚合断言" % len(rows)

    if kind in ("DB", "MQ", "Trace"):
        source = _req(assertion, "来源", kind)
        observation = (resp.get("observations") or {}).get(source)
        if observation is None:
            raise AssertError("「%s」断言没有来源 %s 的可用观测结果" % (kind, source))
        if observation.get("unavailable"):
            raise AssertError("观测不可用：%s" % observation.get("reason", "原因未知"))
        rows = observation.get("_records", observation.get("records")) or []
        if "数量" in assertion:
            ok, detail = judge_count(assertion["数量"], len(rows), variables, "%s记录数" % kind)
            if not ok: return False, "%s 来源 %s：%s（实际 %d）" % (kind, source, detail, len(rows))
        if kind == "MQ" and "事件类型" in assertion:
            want = fill(assertion["事件类型"], variables)
            if any(r.get("eventType") != want for r in rows):
                return False, "MQ 来源 %s 存在事件类型不为 %s 的记录" % (source, want)
        if "字段" in assertion:
            expected = fill(assertion["字段"], variables, keep_types=True)
            for row in rows:
                bad = [k for k, v in expected.items() if deep_get(row, k) != v]
                if bad: return False, "%s 来源 %s 字段不符：%s" % (kind, source, "、".join(bad))
        if kind == "Trace":
            if "错误数" in assertion:
                n = sum(1 for r in rows if str(r.get("status", "")).upper() == "ERROR")
                if n != int(assertion["错误数"]): return False, "Trace 错误 span=%d（期望 %s）" % (n, assertion["错误数"])
            spans = (assertion.get("span") or {}).get("包含") or []
            for wanted in spans:
                if not any(all(deep_get(row, k) == v for k, v in wanted.items()) for row in rows):
                    return False, "Trace 缺少 span %s" % _short(wanted)
        return True, "%s 来源 %s 命中 %d 条记录" % (kind, source, len(rows))

    # ---------------- 字段层
    if kind == "路径存在":
        path = _req(assertion, "路径", kind)
        return got is not None, "%s %s" % (path, "存在" if got is not None else "缺失")

    if kind == "JSON字段":
        path = assertion["路径"]
        if "等于" in assertion:
            # keep_types：期望值来自数据组时必须保留类型，
            # 否则 0 变成 "0"，与响应里的 0 永远不相等（下面 _num_eq 分支就成了死代码）
            want = fill(assertion["等于"], variables, keep_types=True)
            ok = _num_eq(got, want) if isinstance(want, (int, float)) else got == want
            return ok, "%s=%s（期望 %s）" % (path, _short(got), _short(want))
        if "不等于" in assertion:
            want = fill(assertion["不等于"], variables, keep_types=True)
            ok = not (_num_eq(got, want) if isinstance(want, (int, float)) else got == want)
            return ok, "%s=%s（不应为 %s）" % (path, _short(got), _short(want))
        if "正则" in assertion:
            want = fill(assertion["正则"], variables)
            ok = isinstance(got, str) and re.search(want, got) is not None
            return ok, "%s=%s（正则 %s）" % (path, _short(got), want)
        if "长度" in assertion:
            want = _to_num(fill(assertion["长度"], variables))
            n = len(got) if isinstance(got, (list, str, dict)) else None
            ok = want is not None and n is not None and _num_eq(n, want)
            return ok, "%s 长度=%s（期望 %s）" % (path, n, _fmt_num(want, assertion["长度"]))
        if "大于" in assertion:
            want = _to_num(fill(assertion["大于"], variables))
            ok = isinstance(got, (int, float)) and not isinstance(got, bool) \
                and want is not None and got > want
            return ok, "%s=%s（期望 >%s）" % (path, got, _fmt_num(want, assertion["大于"]))
        if "大于等于" in assertion:
            want = _to_num(fill(assertion["大于等于"], variables))
            ok = isinstance(got, (int, float)) and not isinstance(got, bool) \
                and want is not None and got >= want
            return ok, "%s=%s（期望 ≥%s）" % (path, got, _fmt_num(want, assertion["大于等于"]))
        if "小于等于" in assertion:
            want = _to_num(fill(assertion["小于等于"], variables))
            ok = isinstance(got, (int, float)) and not isinstance(got, bool) \
                and want is not None and got <= want
            return ok, "%s=%s（期望 ≤%s）" % (path, got, _fmt_num(want, assertion["小于等于"]))
        if "期望在" in assertion:
            rng = assertion["期望在"]
            if not (isinstance(rng, list) and len(rng) == 2):
                raise AssertError("「期望在」必须是 [下限, 上限]，实际是 %r" % (rng,))
            lo, hi = rng
            lo_n = _to_num(fill(lo, variables))
            hi_n = _to_num(fill(hi, variables))
            ok = isinstance(got, (int, float)) and not isinstance(got, bool) \
                and lo_n is not None and hi_n is not None and lo_n <= got <= hi_n
            return ok, "%s=%s（期望 %s~%s）" % (path, got, _fmt_num(lo_n, lo), _fmt_num(hi_n, hi))
        if "包含" in assertion:
            wanted = [str(fill(v, variables)) for v in _as_list(assertion["包含"])]
            hay = got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
            miss = [w for w in wanted if w not in hay]
            return not miss, "%s 包含检查%s" % (path, "" if not miss else "，缺 %s" % _short(miss))
        if "不包含" in assertion:
            banned = [str(fill(v, variables)) for v in _as_list(assertion["不包含"])]
            hay = got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
            hit = [b for b in banned if b in hay]
            return not hit, "%s 未出现禁用内容" % path if not hit else "%s 出现 %s" % (path, _short(hit))
        if assertion.get("非空"):
            ok = got not in (None, "", [], {})
            return ok, "%s=%s" % (path, "有值" if ok else "为空")
        if assertion.get("为空"):
            ok = got in (None, "", [], {})
            return ok, "%s=%s（期望为空）" % (path, _short(got))
        if assertion.get("是整数"):
            ok = isinstance(got, int) and not isinstance(got, bool)
            return ok, "%s=%r（%s，期望整数）" % (path, got, type(got).__name__)
        if assertion.get("是数组"):
            ok = isinstance(got, list)
            return ok, "%s 是 %s（期望数组）" % (path, type(got).__name__)
        if assertion.get("是布尔"):
            ok = isinstance(got, bool)
            return ok, "%s=%r（%s，期望布尔）" % (path, got, type(got).__name__)
        if assertion.get("是字符串"):
            ok = isinstance(got, str)
            return ok, "%s 是 %s（期望字符串）" % (path, type(got).__name__)
        raise AssertError(
            "JSON字段断言缺少判定条件（等于/不等于/正则/长度/期望在/非空/为空/是数组…）：%r" % (assertion,)
        )

    # ---------------- 产物层（HTTP 与 UI 都能用）
    if kind == "下载":
        if resp.get("ui_unavailable"):
            raise AssertError("浏览器通道不可用，无法判定下载产物：%s"
                              % (resp.get("error") or ""))
        downloads = resp.get("downloads")
        if downloads is None:
            # 没配下载动作却断言下载 —— 这是规格写错，不是产品缺陷。
            raise AssertError(
                "「下载」断言要求该步骤确实下载过文件：UI 步骤写 "
                "{\"ui\":\"下载\",\"选择器\":…,\"下载目录\":…}，"
                "HTTP 步骤写 \"下载目录\": …。当前步骤没有下载动作。")

        file_keys = ("存在", "后缀", "名称包含", "最小字节", "最大字节")
        if "数量" in assertion:
            ok, detail = judge_count(assertion["数量"], len(downloads), variables, "下载数量")
            if not ok:
                return False, "%s（实际 %d 个文件）" % (detail, len(downloads))
            if not any(k in assertion for k in file_keys):
                # 只判个数：这时候不需要产物细节，直接给结论
                return True, "%s（实际 %d 个文件）" % (detail, len(downloads))

        if not downloads:
            return False, "这一步没有收到下载 —— 点击没有触发下载事件"

        d = downloads[-1]
        label = d.get("name") or "(未命名)"
        if d.get("truncated"):
            # 截断的文件落盘后会骗过「最小字节」，所以宁可拒绝判定
            raise AssertError("下载内容超过单文件上限，未落盘（%s），无法判定"
                              % (d.get("error") or ""))
        if d.get("error") and not d.get("path"):
            raise AssertError("下载已触发但保存失败，无法判定：%s" % d["error"])

        checks = []
        if "存在" in assertion:
            want = bool(assertion["存在"])
            exists = bool(d.get("path")) and os.path.isfile(d["path"])
            checks.append((exists == want,
                           "文件%s" % ("已落盘" if exists else "未落盘")))
        if "后缀" in assertion:
            want = str(fill(assertion["后缀"], variables)).strip().lower()
            if want and not want.startswith("."):
                want = "." + want
            got = os.path.splitext(label)[1].lower()
            checks.append((got == want, "文件名 %s 的后缀是 %s（期望 %s）"
                                        % (label, got or "无", want or "无")))
        if "名称包含" in assertion:
            want = str(fill(assertion["名称包含"], variables))
            checks.append((want in label, "文件名 %s（期望含 %s）" % (label, want)))
        if "最小字节" in assertion:
            want = _req_num(assertion, "最小字节", kind, variables)
            size = int(d.get("size") or 0)
            checks.append((size >= want, "%s 字节（期望 ≥%s）"
                                        % (_fmt_num(size), _fmt_num(want))))
        if "最大字节" in assertion:
            want = _req_num(assertion, "最大字节", kind, variables)
            size = int(d.get("size") or 0)
            checks.append((size <= want, "%s 字节（期望 ≤%s）"
                                        % (_fmt_num(size), _fmt_num(want))))
        if not checks:
            raise AssertError("下载断言至少要有 存在/后缀/名称包含/最小字节/最大字节/数量 之一")

        ok = all(c[0] for c in checks)
        detail = "；".join(c[1] for c in checks)
        if d.get("path"):
            detail += " → %s（%s 字节，sha256 %s…）" % (
                d["path"], d.get("size"), (d.get("sha256") or "")[:12])
        return ok, detail

    # ---------------- 页面层（只对 UI 步骤生效）
    if kind in ("页面", "URL", "控制台", "网络"):
        if not resp.get("ui") and not (isinstance(body_json, dict) and body_json.get("elements")):
            raise AssertError(
                "「%s」断言只能挂在 UI 步骤上。当前步骤走的是 HTTP 通道，"
                "响应里没有页面快照。把断言挂到带 ui 动作的步骤，或在后面加一步 "
                "{\"ui\": \"断言快照\"}。" % kind
            )
        page_json = body_json if isinstance(body_json, dict) else {}

        if kind == "URL":
            actual = page_json.get("url") or resp.get("url") or ""
            if "等于" in assertion:
                want = fill(assertion["等于"], variables)
                ok = actual == want
                return ok, "页面 URL=%s（期望 %s）" % (actual, want)
            if "包含" in assertion:
                want = fill(assertion["包含"], variables)
                ok = want in actual
                return ok, "页面 URL=%s（期望含 %s）" % (actual, want)
            if "不含" in assertion:
                want = fill(assertion["不含"], variables)
                ok = want not in actual
                return ok, "页面 URL=%s（不应含 %s）" % (actual, want)
            if "正则" in assertion:
                want = fill(assertion["正则"], variables)
                ok = re.search(want, actual) is not None
                return ok, "页面 URL=%s（正则 %s）" % (actual, want)
            raise AssertError("URL 断言需指定 等于/包含/不含/正则")

        if kind == "控制台":
            errs = list(page_json.get("console_errors") or []) + \
                list(page_json.get("page_errors") or [])
            if assertion.get("无错误") is not None:
                want = assertion["无错误"]
                ok = (len(errs) == 0) == bool(want)
                if ok:
                    return True, "控制台无 JS 错误" if want else "控制台存在 JS 错误（符合预期）"
                return False, ("出现 %d 条 JS 错误：%s" % (len(errs), _short(errs, 200))
                               if want else "期望有 JS 错误，实际没有")
            if "不含" in assertion:
                banned = [str(fill(v, variables)) for v in _as_list(assertion["不含"])]
                hit = [b for b in banned if any(b in e for e in errs)]
                return not hit, ("未出现禁用报错" if not hit
                                 else "控制台出现 %s" % _short(hit, 200))
            raise AssertError("控制台断言需指定 无错误/不含")

        if kind == "网络":
            fails = page_json.get("network_failures") or []
            if assertion.get("无失败请求") is not None:
                allow = [str(fill(v, variables)) for v in _as_list(assertion.get("允许"))]
                real = [f for f in fails
                        if not any(a in (f.get("url") or "") for a in allow)]
                want = assertion["无失败请求"]
                ok = (len(real) == 0) == bool(want)
                if ok:
                    return True, ("无失败请求" if want else "存在失败请求（符合预期）")
                detail = ["%s %s %s" % (f.get("method", ""), f.get("status", ""),
                                        (f.get("url") or "")[-60:]) for f in real[:5]]
                return False, ("出现 %d 条失败请求：%s" % (len(real), "；".join(detail))
                               if want else "期望有失败请求，实际没有")

            target = assertion.get("请求")
            if target:
                return _judge_network_requests(assertion, page_json, variables, target)

            raise AssertError(
                "网络断言需指定「无失败请求」，或「请求」+「次数」/「请求体包含」：%r" % (assertion,)
            )

        # ---- 页面元素
        sel = fill(assertion.get("选择器"), variables)
        if not sel:
            raise AssertError("页面断言需要「选择器」")
        elements = page_json.get("elements") or {}
        if sel not in elements:
            # 浏览器不可用时快照是空的，这时报"选择器不存在"会把人引到错误方向
            upstream = resp.get("error") or ""
            if upstream or resp.get("ui_unavailable"):
                raise AssertError(
                    "UI 断言无法判定：%s（选择器「%s」没有快照数据）"
                    % (upstream or "浏览器通道不可用", sel)
                )
            raise AssertError(
                "选择器「%s」不在本步骤快照范围内。快照只抓用例里出现过的选择器 —— "
                "检查选择器是否拼错，或在该步骤的「等待」里带上它。" % sel
            )
        el = elements[sel]
        if el.get("probe_failed"):
            # 探测本身失败，不等于元素不存在。这两种情况在报告里长得一模一样，
            # 但结论完全相反：一个是产品缺陷，一个是测试时机不对。
            raise AssertError(
                "UI 断言无法判定：探测元素「%s」时出错（%s）。这不是元素不存在 —— "
                "常见原因是页面正在跳转、元素所在 iframe 已卸载、或渲染卡住。"
                "先在该步骤加「等待」确认元素就绪，再断言。"
                % (sel, el.get("error") or "原因未知")
            )
        n = el.get("count", 0)

        if "存在" in assertion:
            want = bool(assertion["存在"])
            ok = bool(el.get("exists")) == want
            return ok, "元素 %s %s（期望%s）" % (
                sel, "存在" if el.get("exists") else "不存在", "存在" if want else "不存在")
        if "可见" in assertion:
            want = bool(assertion["可见"])
            got_v = el.get("visible")
            ok = bool(got_v) == want and n > 0
            return ok, "元素 %s %s（期望%s）" % (
                sel, "可见" if got_v else ("不可见" if n else "不存在"),
                "可见" if want else "不可见")
        if "数量" in assertion:
            want = _to_num(fill(assertion["数量"], variables))
            ok = want is not None and _num_eq(n, want)
            return ok, "元素 %s 数量=%s（期望 %s）" % (sel, n, assertion["数量"])
        if "文本" in assertion:
            want = str(fill(assertion["文本"], variables))
            hay = el.get("text") or ""
            ok = want in hay
            return ok, "元素 %s 文本=%s（期望含 %s）" % (sel, _short(hay, 80), _short(want, 40))
        if "文本等于" in assertion:
            want = str(fill(assertion["文本等于"], variables))
            hay = (el.get("text") or "").strip()
            ok = hay == want.strip()
            return ok, "元素 %s 文本=%s（期望等于 %s）" % (sel, _short(hay, 80), _short(want, 40))
        if "不含文本" in assertion:
            banned = [str(fill(v, variables)) for v in _as_list(assertion["不含文本"])]
            hay = el.get("text") or ""
            hit = [b for b in banned if b in hay]
            return not hit, ("元素 %s 未出现禁用文本" % sel if not hit
                             else "元素 %s 出现 %s" % (sel, _short(hit, 80)))
        if "值" in assertion:
            want = str(fill(assertion["值"], variables))
            got_v = el.get("value")
            ok = (got_v or "") == want
            return ok, "元素 %s 值=%s（期望 %s）" % (sel, _short(got_v, 40), _short(want, 40))
        if "可用" in assertion:
            want = bool(assertion["可用"])
            got_e = el.get("enabled")
            ok = bool(got_e) == want
            return ok, "元素 %s %s（期望%s）" % (
                sel, "可用" if got_e else "禁用", "可用" if want else "禁用")
        if "属性" in assertion:
            want_attrs = fill(assertion["属性"], variables) or {}
            attrs = el.get("attrs") or {}
            miss = ["%s=%s（实际 %s）" % (k, v, attrs.get(k)) for k, v in want_attrs.items()
                    if str(attrs.get(k)) != str(v)]
            return not miss, ("元素 %s 属性匹配" % sel if not miss
                              else "元素 %s 属性不符：%s" % (sel, "；".join(miss)))
        raise AssertError(
            "页面断言缺少判定条件（存在/可见/数量/文本/文本等于/不含文本/值/属性/可用）：%r"
            % (assertion,)
        )

    # 兜底：声明了类型但没给判定条件
    raise AssertError("断言「%s」没有给出判定条件：%r" % (kind, assertion))


def check_assertion_shape(assertion, available_vars=None, where=""):
    """静态校验一条断言写没写全，供 lint / compile 使用。

    返回问题列表（空列表代表通过）。
    """
    problems = []
    available_vars = available_vars or set()
    try:
        kind = assertion.get("类型")
        if not kind:
            return ["%s 断言缺少「类型」字段" % where]
        if kind not in SUPPORTED:
            return ["%s 未知断言类型「%s」" % (where, kind)]

        conditions = [k for k in assertion if k not in ("类型", "路径", "名称", "选择器")]
        if not conditions:
            problems.append("%s 断言「%s」没有判定条件" % (where, kind))

        if kind in ("JSON字段", "路径存在") and "路径" not in assertion:
            problems.append("%s 断言「%s」缺少「路径」" % (where, kind))
        if kind == "响应头" and "名称" not in assertion:
            problems.append("%s 断言「响应头」缺少「名称」" % where)
        if kind == "响应契约":
            has_schema = "schema" in assertion
            has_openapi = any(k in assertion for k in ("来源", "operationId", "响应"))
            if has_schema and has_openapi:
                problems.append("%s 断言「响应契约」的 schema 与 来源/operationId/响应 不能同时使用" % where)
            elif not has_schema and not has_openapi:
                problems.append("%s 断言「响应契约」必须给 schema，或给 来源 + operationId + 响应" % where)
            elif has_openapi and not all(k in assertion for k in ("来源", "operationId", "响应")):
                problems.append("%s 断言「响应契约」的 OpenAPI 写法必须同时给 来源、operationId、响应" % where)
        if kind in ("DB", "MQ", "Trace") and "来源" not in assertion:
            problems.append("%s 断言「%s」缺少「来源」" % (where, kind))
        if kind == "并发结果" and not any(k in assertion for k in ("请求数", "成功数", "失败数", "状态码", "JSON字段")):
            problems.append("%s 断言「并发结果」至少要给一个聚合条件" % where)
        if kind == "页面":
            if "选择器" not in assertion:
                problems.append("%s 断言「页面」缺少「选择器」" % where)
            if all(c in ("选择器",) for c in conditions):
                problems.append("%s 断言「页面」只给了选择器，没有判定条件"
                                "（存在/可见/数量/文本/值/属性/可用）" % where)
        if kind == "网络":
            if "请求" not in assertion and "无失败请求" not in assertion:
                problems.append("%s 断言「网络」既没有「无失败请求」，也没有「请求」" % where)
            if "请求" in assertion and not any(
                    k in assertion for k in ("次数", "请求体包含")):
                problems.append("%s 断言「网络」给了「请求」但没给判定条件"
                                "（次数 / 请求体包含）" % where)
            cnt = assertion.get("次数")
            if isinstance(cnt, dict) and not any(op in cnt for op, _ in _COUNT_OPS):
                problems.append("%s 断言「网络」的「次数」只支持 %s，实际是 %r"
                                % (where, "/".join(o for o, _ in _COUNT_OPS), cnt))
        if kind == "下载":
            for key in ("最小字节", "最大字节"):
                if key in assertion and not str(assertion[key]).lstrip("-").isdigit():
                    problems.append("%s 断言「下载」的「%s」必须是整数，实际是 %r"
                                    % (where, key, assertion[key]))
        if kind == "期望在" or "期望在" in assertion:
            rng = assertion.get("期望在")
            if not (isinstance(rng, list) and len(rng) == 2):
                problems.append("%s 「期望在」必须是 [下限, 上限]" % where)

        # 变量是否在之前步骤提取过
        for key, val in assertion.items():
            for name in re.findall(r"\$\{([\w.\-]+)\}", json.dumps(val, ensure_ascii=False)):
                if name.startswith("env."):
                    continue
                if name not in available_vars:
                    problems.append(
                        "%s 断言引用了未定义的变量 ${%s}（该变量必须在之前的步骤「提取」里出现过）"
                        % (where, name)
                    )
    except AssertError as e:
        problems.append("%s %s" % (where, e))
    return problems
