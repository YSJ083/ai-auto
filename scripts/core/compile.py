"""编译层 —— 把「给人看的用例」编译成「给机器跑的规格」，并检查它写没写全。

这一层是自动测试能不能闭环的关键。用例表本身不可执行，因为它用自然语言描述预期；
编译的实质是把每条「预期结果」翻译成至少一条可判定断言，翻不出来的必须暴露成错误，
绝不能让它悄悄变成一条永远通过的用例。

顺带做 lint：断言是否可判定、变量是否有来源、数据量断言是否会假失败、用例是否重名。
"""

import json
import os
import re
import uuid

from . import contract as contract_mod
from .asserts import SUPPORTED, check_assertion_shape, find_vague
from .coverage import (case_requirements, check_against_declared,
                       normalize_requirements, validate_requirements_shape)

STEP_KEYS = {"名称", "method", "path", "params", "headers", "json", "form", "body", "断言",
             "提取", "no_auth", "timeout", "轮询", "并发", "故障", "观测等待",
             # ---- UI 步骤
             "ui", "动作", "选择器", "值", "键", "超时", "第几个", "等待", "等页面",
             "等网络空闲", "网络空闲超时", "毫秒", "状态", "url",
             # ---- 识别验证码专用
             "保存到", "长度", "图片地址",
             # ---- 文件上传 / 下载（HTTP 与 UI 共用）
             "文件", "files", "下载目录"}
POLL_KEYS = {"直到", "超时", "间隔", "method", "path", "params", "json", "form",
             "body", "headers", "no_auth"}
OBSERVABILITY_KINDS = {"DB", "MQ", "Trace"}
OBSERVABILITY_WAIT_KEYS = {"来源", "直到", "超时", "间隔"}
EXEC_KEYS = {"前置", "步骤", "清理", "变量", "重试", "失败即止", "依赖", "自动化", "说明",
             # 需求点写在执行块里，是为了不动上游 testcase-designer 的 10 字段契约
             "需求点"}

UI_ASSERT_TYPES = ("页面", "URL", "控制台", "网络")
VIRTUAL_ACTIONS = ("delay", "response", "reset")
VIRTUAL_VAR_RE = re.compile(r"\$\{(virtual\.[\w.-]+\.base_url)\}")


def _virtual_refs(value):
    """收集规格中显式使用的虚拟服务地址变量。"""
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _virtual_refs(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _virtual_refs(item)]
    return VIRTUAL_VAR_RE.findall(value) if isinstance(value, str) else []


def _check_virtual_services(services, problems, where="顶层 virtual_services"):
    if services is None:
        return {}
    if not isinstance(services, list) or not services:
        problems.append(("error", "%s 必须是非空数组" % where))
        return {}
    out, names = {}, set()
    for index, service in enumerate(services, 1):
        label = "%s 第 %d 项" % (where, index)
        if not isinstance(service, dict):
            problems.append(("error", "%s 必须是对象" % label)); continue
        name, routes = service.get("name"), service.get("routes")
        if not isinstance(name, str) or not re.match(r"^[A-Za-z][\w-]*$", name):
            problems.append(("error", "%s 的 name 必须是字母开头的标识符" % label)); continue
        if name in names:
            problems.append(("error", "%s 的 name 重复：%s" % (where, name))); continue
        names.add(name)
        if not isinstance(routes, list) or not routes:
            problems.append(("error", "虚拟服务 %s 的 routes 必须是非空数组" % name)); continue
        route_keys = set()
        for route_index, route in enumerate(routes, 1):
            route_label = "虚拟服务 %s 的 routes 第 %d 项" % (name, route_index)
            match = route.get("match") if isinstance(route, dict) else None
            response = route.get("response") if isinstance(route, dict) else None
            if not isinstance(match, dict) or not isinstance(match.get("method"), str) \
                    or not isinstance(match.get("path"), str) or not match["path"].startswith("/"):
                problems.append(("error", "%s 必须有精确 match.method 和以 / 开头的 match.path" % route_label))
                continue
            key = (match["method"].upper(), match["path"])
            if key in route_keys:
                problems.append(("error", "%s 的匹配重复：%s %s" % (route_label, key[0], key[1])))
            route_keys.add(key)
            if not isinstance(response, dict) or not isinstance(response.get("status", 200), int):
                problems.append(("error", "%s 必须有含整数 status 的 response" % route_label))
        out[name] = route_keys
    return out


def _check_virtual_references(value, services, problems, where):
    for ref in _virtual_refs(value):
        name = ref.split(".")[1]
        if name not in services:
            problems.append(("error", "%s 引用了未声明的 ${%s}" % (where, ref)))


def _check_fault(st, where, services, problems):
    fault = st.get("故障")
    if fault is None:
        return
    if _is_ui_step(st):
        problems.append(("error", "%s 的「故障」只支持 HTTP 步骤" % where)); return
    if not isinstance(fault, dict):
        problems.append(("error", "%s 的「故障」必须是对象" % where)); return
    name, match, action = fault.get("服务"), fault.get("match"), fault.get("action")
    if name not in services:
        problems.append(("error", "%s 的故障服务不存在：%s" % (where, name))); return
    if not isinstance(match, dict) or not isinstance(match.get("method"), str) \
            or not isinstance(match.get("path"), str):
        problems.append(("error", "%s 的故障必须指定精确 match.method 和 match.path" % where)); return
    key = (match["method"].upper(), match["path"])
    if key not in services[name]:
        problems.append(("error", "%s 的故障匹配不到虚拟服务 %s 已声明的 route：%s %s" %
                         (where, name, key[0], key[1])))
    if action not in VIRTUAL_ACTIONS:
        problems.append(("error", "%s 的故障 action 只支持 delay、response、reset" % where))
    elif action == "delay" and (not isinstance(fault.get("毫秒"), (int, float)) or fault["毫秒"] < 0):
        problems.append(("error", "%s 的 delay 故障必须配置非负数字「毫秒」" % where))
    elif action == "response":
        response = fault.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("status", 200), int):
            problems.append(("error", "%s 的 response 故障必须配置含整数 status 的 response" % where))
    if not st.get("断言"):
        problems.append(("error", "%s 的故障步骤必须有断言或 DB/MQ/Trace 观测，不能只安装规则" % where))


def _is_ui_step(st):
    return bool(isinstance(st, dict) and (st.get("ui") or st.get("动作")))


def _defined_vars(st):
    """一个步骤会定义哪些变量。

    `提取` 是 HTTP 侧的常规来源；`识别验证码` 通过 `保存到` 写入变量，
    同样是可溯源的定义，否则 `${验证码}` 会被误判成未定义。
    """
    out = list((st.get("提取") or {}).keys())
    if (st.get("ui") or st.get("动作")) == "识别验证码":
        out.append(st.get("保存到") or "验证码")
    return out


def poll_conditions(st):
    """步骤里「轮询.直到」的断言列表（允许写单条）。"""
    poll = st.get("轮询") if isinstance(st, dict) else None
    if not isinstance(poll, dict):
        return []
    until = poll.get("直到")
    if until is None:
        return []
    return list(until) if isinstance(until, list) else [until]


def _check_poll(st, where, problems):
    """校验「轮询」块的形状。

    轮询写错的代价不对称：它会让用例卡满超时（默认 30 秒）再报一条与真实
    问题无关的失败。所以宁可编译期就拦下来，也不要等到执行时才发现。
    """
    poll = st.get("轮询")
    if poll is None:
        return
    if not isinstance(poll, dict):
        problems.append(("error", "%s 的「轮询」必须是对象" % where))
        return
    if _is_ui_step(st):
        problems.append(("error", "%s 是 UI 步骤，「轮询」只支持 HTTP 步骤；"
                                  "UI 等异步渲染请改用「等待」" % where))
    if not st.get("path") and not poll.get("path"):
        problems.append(("error", "%s 配了「轮询」但没有 path（步骤和轮询块里都没有）" % where))
    until = poll.get("直到")
    if not until:
        problems.append(("error", "%s 的「轮询」缺少「直到」—— 没有成立条件就只是重试" % where))
    elif not isinstance(until, (dict, list)):
        problems.append(("error", "%s 的「轮询.直到」必须是断言对象或断言数组" % where))
    for k in ("超时", "间隔"):
        if k in poll and not isinstance(poll[k], (int, float)):
            problems.append(("error", "%s 的「轮询.%s」必须是数字（毫秒）" % (where, k)))
    unknown = set(poll) - POLL_KEYS
    if unknown:
        problems.append(("warning", "%s 的「轮询」含未知字段 %s（会被忽略）"
                         % (where, "、".join(sorted(unknown)))))


def _check_observability(cfg, problems, where="顶层 observability"):
    """校验只读观测 DSL，拒绝任何后端直连或命令式查询配置。"""
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        problems.append(("error", "%s 必须是对象" % where))
        return {}
    gateway = cfg.get("gateway")
    sources = cfg.get("sources")
    unknown = set(cfg) - {"gateway", "sources"}
    if unknown:
        problems.append(("error", "%s 含不支持字段 %s；只允许 gateway 和 sources" %
                         (where, "、".join(sorted(unknown)))))
    if not isinstance(gateway, dict):
        problems.append(("error", "%s.gateway 必须是对象" % where))
    else:
        base = gateway.get("base_url")
        if not isinstance(base, str) or not base.startswith(("http://", "https://")):
            problems.append(("error", "%s.gateway.base_url 必须是 HTTP(S) 只读网关地址" % where))
        if "timeout_ms" in gateway and (not isinstance(gateway["timeout_ms"], (int, float))
                                         or isinstance(gateway["timeout_ms"], bool)
                                         or gateway["timeout_ms"] <= 0):
            problems.append(("error", "%s.gateway.timeout_ms 必须是正数毫秒" % where))
        bad = set(gateway) - {"base_url", "timeout_ms"}
        if bad:
            problems.append(("error", "%s.gateway 不允许字段 %s；不支持 SQL/MQ 命令/TraceQL 或后端直连配置" %
                             (where, "、".join(sorted(bad)))))
    if not isinstance(sources, dict) or not sources:
        problems.append(("error", "%s.sources 必须是非空对象" % where))
        return {}
    out = {}
    for name, source in sources.items():
        label = "%s.sources.%s" % (where, name)
        if not isinstance(name, str) or not name:
            problems.append(("error", "%s 的来源名必须是非空字符串" % where)); continue
        if not isinstance(source, dict):
            problems.append(("error", "%s 必须是对象" % label)); continue
        if source.get("kind") not in OBSERVABILITY_KINDS:
            problems.append(("error", "%s.kind 必须是 DB/MQ/Trace" % label)); continue
        extras = set(source) - {"kind"}
        if extras:
            problems.append(("error", "%s 不允许字段 %s；来源只能声明 kind，查询由只读网关预声明" %
                             (label, "、".join(sorted(extras)))))
        out[name] = source.get("kind")
    return out


def _check_observation_assertion(assertion, where, problems, sources):
    if not isinstance(assertion, dict) or assertion.get("类型") not in OBSERVABILITY_KINDS:
        return
    kind = assertion["类型"]
    source = assertion.get("来源")
    if source not in sources:
        problems.append(("error", "%s 的「%s」断言引用未声明来源：%s" % (where, kind, source)))
    elif sources[source] != kind:
        problems.append(("error", "%s 的「%s」断言来源 %s 的类型是 %s" %
                         (where, kind, source, sources[source])))


def _check_observation_wait(st, where, problems, sources):
    wait = st.get("观测等待")
    if wait is None:
        return
    if not isinstance(wait, dict):
        problems.append(("error", "%s 的「观测等待」必须是对象" % where)); return
    unknown = set(wait) - OBSERVABILITY_WAIT_KEYS
    if unknown:
        problems.append(("error", "%s 的「观测等待」含未知字段 %s" %
                         (where, "、".join(sorted(unknown)))))
    source = wait.get("来源")
    if source not in sources:
        problems.append(("error", "%s 的「观测等待.来源」未在 observability.sources 声明：%s" %
                         (where, source)))
    until = wait.get("直到")
    if not isinstance(until, (dict, list)) or not until:
        problems.append(("error", "%s 的「观测等待」必须给非空「直到」观测断言" % where))
    elif isinstance(until, list) and not all(isinstance(a, dict) for a in until):
        problems.append(("error", "%s 的「观测等待.直到」必须是断言对象或断言数组" % where))
    for key in ("超时", "间隔"):
        if key in wait and (not isinstance(wait[key], (int, float))
                            or isinstance(wait[key], bool) or wait[key] < 0):
            problems.append(("error", "%s 的「观测等待.%s」必须是非负数字（毫秒）" % (where, key)))


def _check_artifacts(st, where, problems):
    """校验上传 / 下载相关字段。

    这两类写错的后果不是「报错」而是「判不了」或「读到错的文件」：上传漏了
    「文件」到执行时才炸，下载断言配在没有下载的步骤上会得到一句
    「需要该步骤确实下载过」。这些都能在编译期就说清楚。
    """
    act = st.get("ui") or st.get("动作") or ""
    files = st.get("文件") if st.get("文件") is not None else st.get("files")

    if act == "上传":
        if not files:
            problems.append(("error", "%s 是「上传」动作但没有「文件」（本地文件路径）" % where))
        elif not isinstance(files, (str, list, dict)):
            problems.append(("error", "%s 的「文件」必须是路径、路径数组或 {字段名: 路径}，实际是 %r"
                             % (where, type(files).__name__)))
        if not st.get("选择器"):
            problems.append(("error", "%s 是「上传」动作但没有「选择器」（指向 input[type=file]）"
                             % where))
    elif files:
        problems.append(("warning", "%s 写了「文件」但不是「上传」动作，会被忽略" % where))

    if act == "下载" and _is_ui_step(st) and (st.get("path") or st.get("url")):
        problems.append(("warning",
                         "%s 同一步既导航又下载。建议拆成两步：先导航打开页面，"
                         "再用单独一步点下载 —— 否则等的是导航完成而不是下载事件" % where))

    dest = st.get("下载目录")
    if dest and act and act != "下载":
        problems.append(("warning", "%s 写了「下载目录」但动作是「%s」，会被忽略" % (where, act)))

    wants = [a for a in (st.get("断言") or [])
             if isinstance(a, dict) and a.get("类型") == "下载"]
    if wants and not (act == "下载" or dest):
        problems.append(("error",
                         "%s 有「下载」断言，但这一步没有下载动作："
                         "UI 步骤写 {\"ui\":\"下载\",…}，HTTP 步骤写 \"下载目录\"。"
                         "没有产物就没有可判的东西" % where))
    if len(wants) > 1:
        problems.append(("warning", "%s 有 %d 条「下载」断言，都判同一个（最后一个）产物"
                         % (where, len(wants))))


def _check_concurrency(st, where, problems):
    cfg = st.get("并发")
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        problems.append(("error", "%s 的「并发」必须是对象" % where)); return
    if _is_ui_step(st) or st.get("轮询") or st.get("文件") or st.get("下载目录"):
        problems.append(("error", "%s 的「并发」只支持无轮询、无上传下载的 HTTP 步骤" % where))
    total, workers = cfg.get("请求数"), cfg.get("并发度")
    if not isinstance(total, int) or not 2 <= total <= 50:
        problems.append(("error", "%s 的「并发.请求数」必须是 2~50 的整数" % where))
    if not isinstance(workers, int) or not isinstance(total, int) or not 1 <= workers <= total:
        problems.append(("error", "%s 的「并发.并发度」必须是 1~请求数 的整数" % where))
    if str(st.get("method", "GET")).upper() in ("POST", "PUT", "PATCH", "DELETE"):
        idem = cfg.get("幂等") or {}
        if not isinstance(idem, dict) or not idem.get("header") or idem.get("模式") != "共享":
            problems.append(("error", "%s 的并发写请求必须配置「幂等」且模式为「共享」" % where))


def _gen_id(index, module):
    return "TC-%03d" % index


def _norm_steps(steps, where, problems, virtual_services=None, observability_sources=None):
    if not isinstance(steps, list):
        problems.append(("error", "%s 的步骤必须是数组" % where))
        return []
    out = []
    for i, st in enumerate(steps, 1):
        sw = "%s 第 %d 步" % (where, i)
        if not isinstance(st, dict):
            problems.append(("error", "%s 不是对象" % sw))
            continue
        if not st.get("path") and not _is_ui_step(st):
            problems.append(("error", "%s 缺少 path（HTTP 步骤必须给；UI 步骤用 ui + 选择器）" % sw))
        if _is_ui_step(st) and not (st.get("选择器") or st.get("ui") in ("等待", "按键", "断言快照")):
            problems.append(("error", "%s 是 UI 动作但没有「选择器」" % sw))
        unknown = set(st) - STEP_KEYS
        if unknown:
            problems.append(("warning", "%s 含未知字段 %s（会被忽略）" % (sw, "、".join(sorted(unknown)))))
        _check_poll(st, sw, problems)
        _check_concurrency(st, sw, problems)
        _check_fault(st, sw, virtual_services or {}, problems)
        _check_observation_wait(st, sw, problems, observability_sources or {})
        _check_virtual_references(st, virtual_services or {}, problems, sw)
        _check_artifacts(st, sw, problems)
        if not st.get("断言") and not poll_conditions(st):
            problems.append(("error", "%s 没有断言 —— 没有断言的步骤只能证明「接口没崩」，不能证明「行为正确」" % sw))
        out.append(st)
    return out


def _abs_credential_paths(auth, base_dir):
    """把凭据文件的相对路径锚定到用例文件所在目录，避免编译后换目录找不到。"""
    import copy
    import os

    out = copy.deepcopy(auth or {})
    if not base_dir:
        return out
    targets = [out]
    if isinstance(out.get("login"), dict):
        targets.append(out["login"])
    for cfg in targets:
        for k in ("token_from", "cookie_from", "value_from", "file",
                  "storage_state", "state_file", "save_state"):
            v = cfg.get(k)
            if v and isinstance(v, str) and not os.path.isabs(v):
                cfg[k] = os.path.normpath(os.path.join(base_dir, v))
    return out


def compile_cases(payload, name=None, base_url="", api_prefix="", env="default",
                  auth=None, vars_=None, spec_version="1.0", base_dir=None):
    """用例 JSON → 规格 JSON。payload 可以是 {meta,cases} 或 纯数组。"""
    problems = []
    if isinstance(payload, list):
        meta, cases = {}, payload
    elif isinstance(payload, dict):
        meta = payload.get("meta") or {}
        cases = payload.get("cases") or []
    else:
        raise ValueError("用例文件必须是数组或 {meta,cases} 对象")

    if not cases:
        raise ValueError("用例文件里没有 cases")

    # 需求清单写在用例文件顶层（跟着用例走，不另起一个文件）
    declared_ids = [d["id"] for d in normalize_requirements(meta)]
    declared_set = set(declared_ids)

    out_cases = []
    seen_titles = {}
    virtual_services = _check_virtual_services(meta.get("virtual_services"), problems)
    observability_sources = _check_observability(meta.get("observability"), problems)
    try:
        contract_registry = contract_mod.build_registry(meta.get("contract"), base_dir)
    except contract_mod.ContractSpecError as e:
        problems.append(("error", "契约配置错误：%s" % e))
        contract_registry = {"schemas": {}, "sources": {}, "max_errors": 20}
    for idx, c in enumerate(cases, 1):
        title = c.get("用例标题") or c.get("title") or ""
        cid = c.get("id") or _gen_id(idx, c.get("所属模块"))
        where = "%s「%s」" % (cid, title[:26])

        module = c.get("所属模块") or c.get("模块") or "未分组"
        auto = c.get("自动化") or c.get("auto") or "auto"
        execu = c.get("执行") or {}

        if auto == "manual" or execu.get("自动化") == "manual":
            for lv, msg in validate_requirements_shape(execu.get("需求点"), where):
                problems.append((lv, msg))
            for lv, msg in check_against_declared(execu, declared_set, where):
                problems.append((lv, msg))
            out_cases.append({
                "id": cid, "模块": module, "用例标题": title,
                "优先级": c.get("优先级") or "P2", "自动化": "manual",
                "依据": c.get("判定依据") or c.get("前置条件") or "",
                "阻塞原因": execu.get("说明") or "规格声明为人工执行",
                "需求点": case_requirements(execu),
                "steps": [],
                "_原文": {"测试步骤": c.get("测试步骤", ""), "预期结果": c.get("预期结果", "")},
            })
            continue

        if not execu:
            problems.append(("error", "%s 是自动化用例但没有「执行」块 —— 无法执行" % where))
            continue
        if "步骤" not in execu:
            problems.append(("error", "%s 的执行块缺少「步骤」" % where))
            continue

        steps = _norm_steps(execu["步骤"], where, problems, virtual_services, observability_sources)
        setup = _norm_steps(execu.get("前置") or [], where + " 前置", problems, virtual_services, observability_sources) if execu.get("前置") else []
        teardown = _norm_steps(execu.get("清理") or [], where + " 清理", problems, virtual_services, observability_sources) if execu.get("清理") else []

        # 需求点：形状错、或引用了规格没声明的 ID，都在编译期拦。
        # 拖到跑完才发现，覆盖度已经少算一截，而报告看上去毫无异常。
        for lv, msg in validate_requirements_shape(execu.get("需求点"), where):
            problems.append((lv, msg))
        for lv, msg in check_against_declared(execu, declared_set, where):
            problems.append((lv, msg))

        # 数据组：每行一组参数，执行时展开成多条。形状先在这拦，
        # 到了执行期才发现形状不对，等于白跑一轮才报错。
        data = c.get("数据") or execu.get("数据") or []
        if data and (not isinstance(data, list) or not all(isinstance(r, dict) for r in data)):
            problems.append(("error", "%s 的「数据」必须是对象数组，每行一组参数" % where))
            data = []

        # 变量可溯源检查：断言里引用的 ${x} 必须在之前的 提取 里出现过
        available = set((vars_ or {}).keys())
        available.update("virtual.%s.base_url" % name for name in virtual_services)
        available.update((execu.get("变量") or {}).keys())
        for row in data:
            available.update(row.keys())
        for st in setup + steps:
            available.update(_defined_vars(st))
            sw = "%s 步骤「%s」" % (where, st.get("名称", ""))
            for a in st.get("断言") or []:
                _check_observation_assertion(a, sw, problems, observability_sources)
                problems.extend(("error", p) for p in check_assertion_shape(a, available, sw))
                if isinstance(a, dict) and a.get("类型") == "响应契约":
                    if _is_ui_step(st):
                        problems.append(("error", "%s 的「响应契约」只能挂在 HTTP 步骤上" % sw))
                    elif not meta.get("contract"):
                        problems.append(("error", "%s 使用了「响应契约」，但顶层没有 contract.schemas 或 contract.sources" % sw))
                    else:
                        try:
                            contract_mod.validate_assertion(a, contract_registry)
                        except contract_mod.ContractSpecError as e:
                            problems.append(("error", "%s 的响应契约无效：%s" % (sw, e)))
            for a in poll_conditions(st):
                problems.extend(("error", p) for p in
                                check_assertion_shape(a, available, sw + " 的轮询直到"))
            wait = st.get("观测等待") or {}
            for a in ([wait.get("直到")] if isinstance(wait.get("直到"), dict)
                      else (wait.get("直到") or [])):
                _check_observation_assertion(a, sw + " 的观测等待直到", problems, observability_sources)
                problems.extend(("error", p) for p in
                                check_assertion_shape(a, available, sw + " 的观测等待直到"))

        # 预期结果的可判定性
        expect = c.get("预期结果") or ""
        if expect:
            hit = find_vague(expect)
            if hit:
                problems.append(("error", "%s 的预期结果含不可判定表述 %s，必须写成具体观测值" % (where, hit)))
            if len(expect) <= 12 and not re.search(r"\d", expect):
                problems.append(("warning", "%s 的预期结果过短，可能缺少观测点" % where))

        if title in seen_titles:
            problems.append(("error", "%s 用例标题与 %s 重复（标题要求模块内唯一）" % (where, seen_titles[title])))
        seen_titles[title] = cid

        if not c.get("优先级"):
            problems.append(("warning", "%s 缺优先级，按 P2 处理" % where))

        out_cases.append({
            "id": cid,
            "模块": module,
            "用例标题": title,
            "优先级": c.get("优先级") or "P2",
            "用例类型": c.get("用例类型", ""),
            "适用阶段": c.get("适用阶段", ""),
            "责任人": c.get("责任人", ""),
            "自动化": "auto",
            "依据": c.get("判定依据") or c.get("前置条件") or "",
            "缺陷等级": c.get("缺陷等级", ""),
            "失败即止": execu.get("失败即止", True),
            "依赖": execu.get("依赖") or [],
            "重试": execu.get("重试", 0),
            "vars": execu.get("变量") or {},
            "需求点": case_requirements(execu),
            "setup": setup or None,
            "steps": steps,
            "teardown": teardown or None,
            "_原文": {"测试步骤": c.get("测试步骤", ""), "预期结果": expect},
        })
        if data:
            # 只有真的要用数据驱动才写这个键，否则每份规格里都挂一个空数组，
            # 读的人还得逐个确认「是不是漏配了」
            out_cases[-1]["数据"] = data

    spec = {
        "name": name or meta.get("name") or "自动测试规格",
        "spec_version": spec_version,
        "env": env,
        "base_url": base_url or meta.get("base_url", ""),
        "api_prefix": api_prefix or meta.get("api_prefix", ""),
        "auth": _abs_credential_paths(auth or meta.get("auth") or {"type": "none"}, base_dir),
        "vars": dict(vars_ or meta.get("vars") or {}),
        "cases": out_cases,
    }
    if meta.get("virtual_services") is not None:
        spec["virtual_services"] = meta["virtual_services"]
    if meta.get("contract"):
        spec["contract"] = meta["contract"]
    if meta.get("observability") is not None:
        spec["observability"] = meta["observability"]
    # 没声明需求清单就不写这个键：老规格不该因为升级多出一个空数组
    if declared_ids:
        spec["需求"] = normalize_requirements(meta)
    # 带 error 的用例不进规格（宁可少一条，也不让有问题的用例混进去假装跑过）
    bad_ids = set()
    for lv, msg in problems:
        if lv == "error":
            m = re.match(r"(TC-[\w.-]+)", msg)
            if m:
                bad_ids.add(m.group(1))
    spec["cases"] = [c for c in out_cases if c["id"] not in bad_ids]
    # 声明了需求却一条都没标：报告会显示「覆盖 0/15」，读的人以为大面积漏测
    if declared_ids and not any(c.get("需求点") for c in spec["cases"]):
        problems.append(("warning",
                         "声明了 %d 条需求，但没有任何用例标「需求点」—— 覆盖度会全是「未覆盖」。"
                         "要么给用例补需求点，要么先删掉「需求」清单" % len(declared_ids)))
    return spec, problems


def lint_spec(spec, base_dir=None):
    """检查一份规格能不能跑。返回 [(level, message)]。"""
    if not isinstance(spec, dict):
        return [("error", "规格必须是 JSON 对象")]
    problems = []
    virtual_services = _check_virtual_services(spec.get("virtual_services"), problems)
    observability_sources = _check_observability(spec.get("observability"), problems)
    _check_virtual_references(spec, virtual_services, problems, "规格")
    try:
        contract_registry = contract_mod.build_registry(spec.get("contract"), base_dir)
    except contract_mod.ContractSpecError as e:
        contract_registry = {"schemas": {}, "sources": {}, "max_errors": 20}
        problems.append(("error", "契约配置错误：%s" % e))
    if not spec.get("cases"):
        problems.append(("error", "规格没有 cases"))
        return problems

    base = (spec.get("base_url") or "").rstrip("/") + (spec.get("api_prefix") or "")
    has_env = bool(spec.get("environments"))
    if not base and not has_env:
        problems.append(("error", "规格与 environments 都没有给出 base_url"))

    auth = spec.get("auth") or {}
    atype = auth.get("type")
    if atype == "form_login" and not (auth.get("login") or {}).get("path"):
        problems.append(("error", "auth.type=form_login 但缺少 auth.login.path"))
    if atype in ("browser_login", "playwright"):
        has_state = bool(auth.get("storage_state") or auth.get("state_file"))
        has_steps = bool(auth.get("login_steps") or auth.get("login_url") or auth.get("login_path"))
        if not has_state and not has_steps:
            problems.append(("error",
                             "auth.type=browser_login 但既没配 storage_state，也没配 login_url/login_steps。"
                             "无法自动登录 —— 用 `auto_test.py login` 人工存盘，或补 login_steps。"))
        if not (auth.get("token_from") or auth.get("extract")) and not has_state:
            problems.append(("warning",
                             "browser_login 没有 token_from：如果系统把 token 存在 localStorage，"
                             "HTTP 通道会拿不到授权头，需要配 token_from（如 localStorage:app-sec-at）。"))
        if not auth.get("probe"):
            problems.append(("warning",
                             "browser_login 建议配 auth.probe：登录态过期时能立刻判为环境阻断，"
                             "而不是让所有用例集体失败。"))
    if atype in ("header", "bearer") and not any(
            k in auth for k in ("token", "token_env", "token_from")):
        problems.append(("warning", "auth 未给出凭据来源，执行时可能因缺凭据被阻断"))

    ids = set()
    declared_ids = set(d["id"] for d in normalize_requirements(spec))
    if spec.get("需求") is not None and not isinstance(spec.get("需求"), list):
        problems.append(("error", "规格顶层的「需求」必须是数组（ID 字符串或 {id,说明} 对象）"))
    for c in spec["cases"]:
        cid = c.get("id")
        if not cid:
            problems.append(("error", "存在没有 id 的用例"))
            continue
        if cid in ids:
            problems.append(("error", "用例 id 重复：%s" % cid))
        ids.add(cid)

        # 需求点：手写规格也要过同一套检查，不能只在 compile 路径上拦
        for lv, msg in validate_requirements_shape(c.get("需求点"), cid):
            problems.append((lv, msg))
        for lv, msg in check_against_declared(c, declared_ids, cid):
            problems.append((lv, msg))

        # 数据组形状：这里拦下来，比跑到一半才发现形状不对强
        data = c.get("数据")
        if data is not None:
            if not isinstance(data, list) or not data:
                problems.append(("error", "%s 的「数据」必须是非空数组；不用数据驱动就删掉这个键" % cid))
                data = []
            elif not all(isinstance(r, dict) for r in data):
                problems.append(("error", "%s 的「数据」每行必须是对象（键值对），不能是标量" % cid))
                data = [r for r in data if isinstance(r, dict)]

        if c.get("自动化") == "manual":
            continue
        if not c.get("steps"):
            problems.append(("error", "%s 没有步骤" % cid))
            continue

        variables = set((spec.get("vars") or {}).keys())
        variables.update("virtual.%s.base_url" % name for name in virtual_services)
        variables.update((c.get("vars") or {}).keys())
        for row in data or []:
            variables.update(row.keys())
        prev_ui = False
        for st in (c.get("setup") or []) + c["steps"] + (c.get("teardown") or []):
            label = "%s / %s" % (cid, st.get("名称") or st.get("path", "") or st.get("ui", ""))
            is_ui = _is_ui_step(st)
            if not st.get("path") and not is_ui:
                problems.append(("error", "%s 缺少 path（HTTP 步骤必须给；UI 步骤用 ui + 选择器）" % label))
            if is_ui and not (st.get("选择器") or st.get("图片地址")
                              or st.get("ui") in ("等待", "按键", "断言快照")):
                problems.append(("error", "%s 是 UI 动作但没有「选择器」" % label))
            if not st.get("断言") and not poll_conditions(st):
                problems.append(("error", "%s 没有断言" % label))
            _check_poll(st, label, problems)
            _check_concurrency(st, label, problems)
            _check_fault(st, label, virtual_services, problems)
            _check_observation_wait(st, label, problems, observability_sources)
            _check_virtual_references(st, virtual_services, problems, label)
            _check_artifacts(st, label, problems)
            for var in (st.get("提取") or {}):
                variables.add(var)
            variables.update(_defined_vars(st))
            for a in poll_conditions(st):
                for p in check_assertion_shape(a, variables, label + " 的轮询直到"):
                    problems.append(("error", p))
            wait = st.get("观测等待") or {}
            for a in ([wait.get("直到")] if isinstance(wait.get("直到"), dict)
                      else (wait.get("直到") or [])):
                _check_observation_assertion(a, label + " 的观测等待直到", problems, observability_sources)
                for p in check_assertion_shape(a, variables, label + " 的观测等待直到"):
                    problems.append(("error", p))
            for a in st.get("断言") or []:
                _check_observation_assertion(a, label, problems, observability_sources)
                for p in check_assertion_shape(a, variables, label):
                    problems.append(("error", p))
                if isinstance(a, dict) and a.get("类型") == "响应契约":
                    if is_ui:
                        problems.append(("error", "%s 的「响应契约」只能挂在 HTTP 步骤上" % label))
                    elif not spec.get("contract"):
                        problems.append(("error", "%s 使用了「响应契约」，但顶层没有 contract.schemas 或 contract.sources" % label))
                    else:
                        try:
                            contract_mod.validate_assertion(a, contract_registry)
                        except contract_mod.ContractSpecError as e:
                            problems.append(("error", "%s 的响应契约无效：%s" % (label, e)))
                # 页面类断言必须挂在 UI 步骤上，否则执行时必定报规格错误
                if isinstance(a, dict) and a.get("类型") in UI_ASSERT_TYPES \
                        and not is_ui and not prev_ui:
                    problems.append((
                        "error",
                        "%s 的「%s」断言挂在了 HTTP 步骤上，前面也没有 UI 步骤。"
                        "页面断言只能判浏览器状态，请挂到带 ui 动作的步骤。" % (label, a.get("类型"))
                    ))
                # 数据量断言：写死等于某个整数，环境数据一变就假失败
                if a.get("类型") == "JSON字段" and isinstance(a.get("等于"), int) \
                        and not isinstance(a.get("等于"), bool) and a["等于"] > 1:
                    path = str(a.get("路径", "")).lower()
                    if any(k in path for k in ("total", "count", "records", "length", "num")):
                        problems.append(("warning", "%s 用等于 %s 断言数量，数据变化会造成假失败，"
                                                    "建议改成「期望在」范围断言" % (label, a["等于"])))
            prev_ui = is_ui

        for dep in c.get("依赖") or []:
            base_dep, _, idx = str(dep).partition("#")
            target = next((x for x in spec["cases"] if x.get("id") == base_dep), None)
            if target is None:
                problems.append(("error", "%s 依赖了不存在的用例 %s" % (cid, dep)))
            elif idx:
                # 依赖到具体某一组数据：TC-X#2 要求 TC-X 确实有这么多组
                total = len(target.get("数据") or [])
                if not total:
                    problems.append(("error", "%s 依赖 %s，但 %s 不是数据驱动用例"
                                     % (cid, dep, base_dep)))
                elif not idx.isdigit() or not 1 <= int(idx) <= total:
                    problems.append(("error", "%s 依赖 %s，但 %s 只有 %d 组数据"
                                     % (cid, dep, base_dep, total)))

    # 声明了需求却一条都没标：报告会显示「覆盖 0/15」，看着像大面积漏测，其实是没标
    if declared_ids and not any(c.get("需求点") for c in spec["cases"]):
        problems.append(("warning",
                         "声明了 %d 条需求，但没有任何用例标「需求点」—— 覆盖度会全是「未覆盖」"
                         % len(declared_ids)))
    return problems


def compile_and_save(payload, out_path, **kw):
    spec, problems = compile_cases(payload, **kw)
    spec["spec_version"] = kw.get("spec_version", "1.0")
    spec["_compile"] = {
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "compiler": "ai-auto-test/compile",
        "trace_id": uuid.uuid4().hex[:8],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
    return spec, problems


def summarize_problems(problems):
    err = [p for p in problems if p[0] == "error"]
    warn = [p for p in problems if p[0] == "warning"]
    return err, warn
