"""执行引擎 —— 把可执行规格跑成结果模型。

这一层只做四件事：发请求、判断言、提变量、定状态。所有「怎么算通过」的知识都在规格里，
引擎不掺业务判断，这样同一份代码换上任意 spec 都能用。

状态语义（报告和 CI 门禁都依赖它，不能混）：
  passed   全部断言通过
  failed   有断言不通过，且响应是真实业务响应 —— 缺陷候选
  blocked  请求根本没完成（连不上/超时/凭据失效）—— 环境问题，不算产品缺陷
  skipped  依赖的用例没通过，本条没执行
  manual   规格里标了自动化=manual，或声明了需要人判
"""

import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import auth as auth_mod
from . import contract as contract_mod
from . import observe as observe_mod
from . import safety as safety_mod
from . import virtual as virtual_mod
from . import env as env_mod
from . import ui as ui_mod
from .asserts import AssertError, deep_get, fill, judge
from .coverage import build_coverage, summarize as summarize_coverage
from .httpc import HttpClient

TOOL = "ai-auto-test"
# 与 skill 目录下的 CHANGELOG.md 保持一致；会写进 result.json 的 tool.version，
# 用于跨轮次对比时判断「结果差异是系统变了还是工具变了」。
VERSION = "2.3.0"
RESULT_SCHEMA = "autotest.result/2"

STATUS_LABEL = {
    "passed": "通过",
    "failed": "失败",
    "blocked": "阻断",
    "skipped": "跳过",
    "manual": "手动",
}

# 数据驱动展开后的 id 分隔符：TC-LOGIN-001#2 表示同一条用例的第 2 组数据。
# 用后缀而不是改标题，是为了让 `--only TC-LOGIN-001` 能把整组一起选中，
# 报告里也能一眼看出「这几行是同一条用例的不同数据」。
DATA_SEP = "#"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    if "cases" not in spec or not isinstance(spec["cases"], list):
        raise ValueError("规格缺少 cases 数组：%s" % path)
    seen = set()
    for c in spec["cases"]:
        cid = c.get("id")
        if not cid:
            raise ValueError("用例缺少 id：%r" % (c.get("用例标题"),))
        if cid in seen:
            raise ValueError("用例 id 重复：%s" % cid)
        seen.add(cid)
    return spec


def _expand_cases(cases):
    """把带 `数据` 的用例展开成多行，一行一组数据。

    三个刻意的取舍：
      - id 加 `#序号` 后缀，不改标题。报告里能看出是同一用例的第几组，
        `--only TC-X` 也能把整组一起选中。
      - `数据` 里的键写进 vars，且**覆盖**用例级 vars —— 数据组是更具体的那一层，
        反过来会让「用例里写死的公共值」永远盖住数据，等于没生效。
      - 保留 `_data` 元信息（第几组/共几组/原始值），报告和 JUnit 都要用它区分行。
    """
    out = []
    for c in cases:
        data = c.get("数据")
        if not data:
            out.append(c)
            continue
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ValueError("用例 %s 的「数据」必须是非空对象数组，实际是 %r"
                             % (c.get("id"), data))
        for i, row in enumerate(data, 1):
            cc = dict(c)
            cc.pop("数据", None)
            cc["id"] = "%s%s%d" % (c["id"], DATA_SEP, i)
            merged = dict(c.get("vars") or {})
            merged.update(row)
            cc["vars"] = merged
            cc["_data"] = {"index": i, "total": len(data),
                           "values": row, "base_id": c["id"]}
            out.append(cc)
    return out


def _dep_states(dep, status_by_id):
    """一个依赖 id 对应的实际状态列表。

    依赖写的是展开前的 id（TC-X）时，要求它展开出来的**每一行**都通过：
    只过一半说明这组数据里已经出现了不确定行为，不该放行后续用例。
    """
    if dep in status_by_id:
        return [status_by_id[dep]]
    hits = [v for k, v in status_by_id.items() if k.startswith(dep + DATA_SEP)]
    return hits or [None]


def _bad_deps(deps, status_by_id):
    return [d for d in deps if any(s != "passed" for s in _dep_states(d, status_by_id))]


def _step_request(client, base, step, variables, auth_headers, case_vars):
    path = fill(step.get("path", ""), variables)
    # 虚拟服务只通过显式 ${virtual.<name>.base_url} 展开为绝对 loopback 地址；
    # 任何其他绝对 URL 都拒绝，避免步骤借路径绕过基准地址访问真实网络目标。
    if path.startswith(("http://", "https://")):
        virtual_urls = [service.base_url for service in
                        (variables.get("__virtual_services") or {}).values()]
        if not any(path == url or path.startswith(url + "/") for url in virtual_urls):
            raise AssertError("绝对 path 只允许使用显式 ${virtual.<name>.base_url} 接入已启动的虚拟服务")
        url = path
    else:
        url = base + path
    step_headers = fill(step.get("headers") or {}, variables)
    headers = dict(auth_headers)
    headers.update(step_headers)
    if step.get("no_auth"):
        # 只摘掉「来自认证配置、且本步没有显式覆盖」的头。
        # 否则「伪造 token」这类用例里显式写的 app-sec-at 会被一起删掉，
        # 请求实际变成无凭据，测出来的就不是伪造场景了。
        for k in list(headers):
            if k in auth_headers and k not in step_headers:
                headers.pop(k)
    params = fill(step.get("params"), variables) if step.get("params") else None
    # 请求体保留类型：数据驱动传的是 18 和 true，写成字符串就变成 "18"/"true"，
    # 服务端要么类型报错、要么按字符串比较，测出来的行为跟真实调用不一样。
    json_body = (fill(step.get("json"), variables, keep_types=True)
                 if step.get("json") is not None else None)
    form = fill(step.get("form"), variables) if step.get("form") is not None else None
    files = fill(step.get("文件", step.get("files")), variables) if (
        step.get("文件") or step.get("files")) else None
    # 下载目录：规格里给了就用它，空值落回本次输出目录下的「下载」子目录。
    # 不设默认值的话产物会散在调用方的工作目录里，跑完谁都找不到。
    dest = None
    if step.get("下载目录"):
        dest = str(fill(step["下载目录"], variables) or "").strip() \
            or getattr(client, "download_dir", "") or None
    return client.request(
        step.get("method", "GET"),
        url,
        headers=headers,
        params=params,
        json_body=json_body,
        form=form,
        raw_body=step.get("body"),
        timeout=step.get("timeout"),
        files=files,
        download_dir=dest,
    )


# ---- 轮询默认值：30 秒、每秒一次。异步造数据的接口通常 2~10 秒落地。
POLL_TIMEOUT_MS = 30000
POLL_INTERVAL_MS = 1000
POLL_MIN_INTERVAL_MS = 50     # 兜住「间隔写 0」，否则退化成死循环


def _poll_conditions(poll):
    """「直到」可以写一条断言，也可以写成断言数组。"""
    until = poll.get("直到")
    if until is None:
        return []
    return list(until) if isinstance(until, list) else [until]


def _poll_request_step(step, poll):
    """把步骤 + 轮询块合成一个纯请求步骤：轮询块里的字段优先。

    断言和提取要摘掉 —— 它们在轮询结束后对「最后一次响应」判一次就够了，
    每次重试都判会让失败信息里出现几十条重复内容。
    """
    req = {k: v for k, v in step.items() if k not in ("轮询", "断言", "提取")}
    for k in ("method", "path", "params", "json", "form", "body", "headers",
              "no_auth", "timeout"):
        if k in poll:
            req[k] = poll[k]
    return req


def _poll_http(client, base, step, variables, auth_headers):
    """反复发同一个请求，直到「直到」全部成立或超时。

    返回 (resp, info, failure)。`resp` 是最后一次响应，后续提取/断言都基于它 ——
    "等状态变成已支付，然后把订单号提出来" 就能写成一步。

    两个刻意的取舍：
      - 中途的 5xx / 连接异常不立即判失败。异步任务本来就可能有短暂的不可用，
        真正的判定交给「超时还没成立」，否则轮询会变成误报制造机。
      - 一次都没拿到真实响应时，resp.status 保持 0，让上层按通道阻断处理，
        而不是当成产品缺陷。
    """
    poll = step["轮询"]
    req = _poll_request_step(step, poll)
    conds = _poll_conditions(poll)
    # 注意：这里必须用 is None 而不是 or —— 「超时: 0」是合法写法（只试一次），
    # 用 or 会被当成没填，回退成 30 秒，然后空转 30 秒。
    timeout_ms = poll.get("超时")
    timeout_ms = POLL_TIMEOUT_MS if timeout_ms is None else max(0, int(timeout_ms))
    interval_ms = poll.get("间隔")
    interval_ms = POLL_INTERVAL_MS if interval_ms is None else int(interval_ms)
    interval_ms = max(POLL_MIN_INTERVAL_MS, interval_ms)
    started = time.time()
    attempts = 0
    last_resp = None
    last_detail = []
    errors = []

    while True:
        attempts += 1
        try:
            resp = _step_request(client, base, req, variables, auth_headers, None)
        except Exception as e:
            resp = {
                "url": req.get("path", ""), "method": req.get("method", "GET"),
                "status": 0, "elapsed_ms": 0, "json": None,
                "text": "轮询请求发不出去：%s" % e,
                "headers": {}, "request_headers": {}, "error": str(e),
            }
        last_resp = resp
        if resp.get("error"):
            errors.append("第 %d 次：%s" % (attempts, resp["error"]))

        ok = True
        last_detail = []
        spec_error = ""
        for a in conds:
            try:
                good, detail = judge(a, resp, variables)
            except AssertError as e:
                spec_error = "轮询「直到」条件写错了：%s" % e
                break
            last_detail.append(detail)
            if not good:
                ok = False
                break       # 一条不成立就不必再判剩下的
        if spec_error:
            return last_resp, {"次数": attempts, "超时": True, "错误": spec_error}, spec_error
        if ok:
            elapsed = int((time.time() - started) * 1000)
            return last_resp, {"次数": attempts, "耗时_ms": elapsed,
                               "直到": last_detail}, None

        elapsed = int((time.time() - started) * 1000)
        if elapsed >= timeout_ms:
            break
        time.sleep(min(interval_ms, max(0, timeout_ms - elapsed)) / 1000.0)

    elapsed = int((time.time() - started) * 1000)
    info = {"次数": attempts, "耗时_ms": elapsed, "超时": True, "直到": last_detail}
    detail = "；".join(last_detail) or "（没有给「直到」条件）"
    if last_resp is not None and last_resp.get("status") == 0:
        failure = ("轮询 %d 次全都没拿到响应（%dms）：%s"
                   % (attempts, timeout_ms, (errors or ["原因未知"])[-1]))
    else:
        failure = ("轮询超时（%dms，共 %d 次，间隔 %dms）：最后一次 %s"
                   % (timeout_ms, attempts, interval_ms, detail))
    return last_resp, info, failure


def _run_concurrent_http(client, base, step, variables, auth_headers, case_id):
    cfg = step["并发"]
    total, workers = cfg["请求数"], cfg["并发度"]
    is_write = str(step.get("method", "GET")).upper() in ("POST", "PUT", "PATCH", "DELETE")
    if is_write:
        safety_mod.require_isolated(variables["__ctx"], "并发写")
    idem = cfg.get("幂等") or {}
    key = fill(idem.get("value", ""), variables) if idem else ""
    if idem and not key:
        key = "%s-%s" % (variables.get("__run_id"), case_id)
    def one(index):
        worker_step = dict(step)
        worker_step.pop("并发", None)
        worker_step["headers"] = dict(step.get("headers") or {})
        if idem:
            worker_step["headers"][idem["header"]] = key
        # 运行关联 ID 由执行器保留：用例作者不能覆盖它，才能把并发请求、
        # 只读观测和服务端审计稳定地关联到同一轮测试。
        worker_step["headers"]["X-Autotest-Run-ID"] = str(variables.get("__run_id") or "")
        worker_step["headers"]["X-Autotest-Case-ID"] = str(case_id)
        worker_step["headers"]["X-Autotest-Worker"] = str(index)
        try:
            return _step_request(client.fork(), base, worker_step, variables, auth_headers, None)
        except Exception as e:
            return {"url": step.get("path", ""), "method": step.get("method", "GET"), "status": 0,
                    "elapsed_ms": 0, "json": None, "text": "并发请求失败：%s" % e,
                    "headers": {}, "request_headers": {}, "error": str(e)}
    started = time.time(); responses = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, i): i for i in range(1, total + 1)}
        try:
            for future in as_completed(futures, timeout=max(1, cfg.get("总超时", 10000) / 1000.0)):
                r = future.result(); r["worker"] = futures[future]; responses.append(r)
        except TimeoutError:
            # 超预算的 future 不会自动带结果回来；记录为 status=0，聚合断言能
            # 清楚显示「完成数不足」，上层再按环境阻断处理，绝不静默丢请求。
            for future, index in futures.items():
                if future.done():
                    continue
                future.cancel()
                responses.append({
                    "url": step.get("path", ""), "method": step.get("method", "GET"),
                    "status": 0, "elapsed_ms": 0, "json": None,
                    "text": "并发请求超过总超时", "headers": {}, "request_headers": {},
                    "error": "并发总超时", "worker": index,
                })
    responses.sort(key=lambda x: x["worker"])
    status_counts = {}
    for r in responses: status_counts[str(r.get("status"))] = status_counts.get(str(r.get("status")), 0) + 1
    first = responses[0] if responses else {"status": 0, "json": None, "text": "并发请求均未完成", "headers": {}, "url": step.get("path", ""), "method": step.get("method", "GET"), "elapsed_ms": 0, "error": "timeout"}
    out = dict(first)
    out["concurrency"] = {"requested": total, "parallelism": workers, "elapsed_ms": int((time.time() - started) * 1000),
                          "responses": responses, "status_counts": status_counts}
    return out


def _collect_observations(observability, assertions, correlation, ctx=None):
    """按来源采集观测，并把网关不可用标记为阻断原因。"""
    observations = {}
    try:
        if ctx is not None:
            safety_mod.require_isolated(ctx, "观测网关",
                                        ((observability.get("gateway") or {}).get("base_url") or ""))
    except safety_mod.SafetyError as e:
        for assertion in assertions:
            observations[assertion.get("来源")] = {"unavailable": True, "reason": str(e)}
        return observations
    for assertion in assertions:
        source = assertion.get("来源")
        if source in observations:
            continue
        try:
            observations[source] = observe_mod.collect(observability, source, correlation)
        except observe_mod.ObservationUnavailable as e:
            observations[source] = {"unavailable": True, "reason": str(e)}
    return observations


def _observation_wait_conditions(wait):
    until = wait.get("直到")
    return [until] if isinstance(until, dict) else list(until or [])


def _wait_for_observations(observability, wait, correlation, variables):
    """在业务断言前等待异步观测落地；通道问题与条件未满足严格分流。"""
    timeout_ms = int(wait.get("超时", POLL_TIMEOUT_MS))
    interval_ms = max(POLL_MIN_INTERVAL_MS, int(wait.get("间隔", POLL_INTERVAL_MS)))
    source = wait.get("来源")
    conditions = _observation_wait_conditions(wait)
    started = time.time()
    attempts = 0
    last = {}
    details = []
    while True:
        attempts += 1
        observations = _collect_observations(observability, [{"来源": source}], correlation,
                                             variables.get("__ctx"))
        observation = observations[source]
        if observation.get("unavailable"):
            return observations, {"来源": source, "次数": attempts, "阻断": True,
                                  "原因": observation["reason"]}, observation["reason"]
        pseudo = {"observations": observations}
        details = []
        satisfied = True
        try:
            for assertion in conditions:
                ok, detail = judge(assertion, pseudo, variables)
                details.append(detail)
                if not ok:
                    satisfied = False
                    break
        except AssertError as e:
            return observations, {"来源": source, "次数": attempts, "规格错误": str(e)}, str(e)
        last = observations
        if satisfied:
            return observations, {"来源": source, "次数": attempts,
                                  "耗时_ms": int((time.time() - started) * 1000),
                                  "直到": details}, None
        elapsed = int((time.time() - started) * 1000)
        if elapsed >= timeout_ms:
            return last, {"来源": source, "次数": attempts, "耗时_ms": elapsed,
                          "超时": True, "直到": details}, (
                "观测等待超时（来源 %s，%dms，共 %d 次）：最后一次 %s" %
                (source, timeout_ms, attempts, "；".join(details)))
        time.sleep(min(interval_ms, max(0, timeout_ms - elapsed)) / 1000.0)
def _run_steps(client, base, steps, variables, auth_headers, stop_on_fail=True,
               ui_session=None, case_id="", ui_base=None):
    """执行一组步骤，返回 (step_results, failures, net_error_only)。

    步骤里带 `ui` 键就走浏览器通道，否则走 HTTP。两条通道产出同形状的响应，
    所以下面的提取/断言/证据逻辑完全共用。

    `base` 带 api_prefix（接口要）；`ui_base` 是纯 origin（页面不要前缀）。
    两者混用会让 /login 变成 /api/login —— 这种错在 URL 断言里看不出来。
    """
    ui_origin = (ui_base or base).rstrip("/")
    step_results = []
    failures = []
    net_error_only = True
    for step in steps:
        name = step.get("名称") or step.get("name") or step.get("path", "")
        poll_info = None
        poll_failure = None
        poll = step.get("轮询")

        observation_wait = step.get("观测等待")
        observation_wait_info = None
        observation_blocked = False
        observation_failure = None
        observations = {}
        observation_assertions = [a for a in step.get("断言") or []
                                  if isinstance(a, dict) and a.get("类型") in ("DB", "MQ", "Trace")]
        correlation = {"run_id": variables.get("__run_id", ""), "case_id": case_id,
                       "step_id": "%s:%s" % (case_id, name)}

        fault_error = ""
        try:
            fault_context = _install_step_fault(step, variables)
        except (safety_mod.SafetyError, virtual_mod.VirtualError, KeyError, TypeError) as e:
            fault_error = str(e)

        if fault_error:
            resp = {
                "url": step.get("path", ""), "method": step.get("method", "GET"), "status": 0,
                "elapsed_ms": 0, "json": None, "text": "虚拟故障被阻断：%s" % fault_error,
                "headers": {}, "request_headers": {}, "error": fault_error,
                "virtual_fault_blocked": True, "virtual_fault_error": fault_error,
            }
        elif step.get("ui") or step.get("动作"):
            if ui_session is None:
                reason = getattr(client, "ui_problem", "") or "浏览器通道不可用"
                resp = {
                    "url": step.get("path", ""), "method": "UI:%s" % (step.get("ui") or step.get("动作")),
                    "status": 0, "elapsed_ms": 0, "json": {"elements": {}}, "text": "",
                    "headers": {}, "request_headers": {},
                    "error": reason,
                    "ui": True, "ui_unavailable": True,
                }
            else:
                resp = ui_mod.run_ui_step(ui_session, step, ui_origin, variables)
            if isinstance(poll, dict):
                # UI 侧已有「等待」（Playwright 的 wait_for），再叠一层轮询只会
                # 让超时叠加、日志变花。这里直接报清楚该用什么，不半途而废。
                poll_info = {"次数": 0, "未执行": True}
                poll_failure = (
                    "「轮询」只支持 HTTP 步骤。UI 步骤要等异步渲染，用「等待」："
                    "{\"等待\": {\"选择器\": \"...\", \"状态\": \"visible\", \"超时\": 8000}}")
        elif isinstance(step.get("并发"), dict):
            try:
                resp = _run_concurrent_http(client, base, step, variables, auth_headers, case_id)
            except (safety_mod.SafetyError, TimeoutError) as e:
                resp = {"url": step.get("path", ""), "method": step.get("method", "GET"), "status": 0,
                        "elapsed_ms": 0, "json": None, "text": "并发执行被阻断：%s" % e,
                        "headers": {}, "request_headers": {}, "error": str(e)}
        elif isinstance(poll, dict):
            resp, poll_info, poll_failure = _poll_http(
                client, base, step, variables, auth_headers)
        else:
            try:
                resp = _step_request(client, base, step, variables, auth_headers, None)
            except Exception as e:
                resp = {
                    "url": step.get("path", ""), "method": step.get("method", "GET"), "status": 0,
                    "elapsed_ms": 0, "json": None, "text": "构造请求失败：%s" % e,
                    "headers": {}, "request_headers": {}, "error": str(e),
                }

        if fault_context is not None:
            _finish_step_fault(fault_context, resp)

        if isinstance(observation_wait, dict):
            waited, observation_wait_info, observation_failure = _wait_for_observations(
                variables.get("__observability") or {}, observation_wait, correlation, variables)
            observations.update(waited)
            observation_blocked = bool(observation_wait_info.get("阻断"))
        if observation_assertions:
            observations.update(_collect_observations(
                variables.get("__observability") or {}, observation_assertions, correlation,
                variables.get("__ctx")))
            observation_blocked = observation_blocked or any(
                item.get("unavailable") for item in observations.values())
        if observations:
            resp["observations"] = observations

        # 提取要先于断言：同一步骤里「records 长度等于 total」这类自洽断言，
        # 依赖的就是本步骤响应里的字段，晚一步提取就取不到值。
        extract_fail = False
        for var, path in (step.get("提取") or {}).items():
            if path is None:
                variables[var] = resp["text"]
            elif isinstance(path, str) and path.startswith("header:"):
                variables[var] = (resp.get("headers") or {}).get(path.split(":", 1)[1])
            elif isinstance(path, str) and path.startswith("cookie:"):
                variables[var] = client.cookies.get(path.split(":", 1)[1])
            elif isinstance(path, str) and path.startswith("status"):
                variables[var] = resp["status"]
            elif isinstance(path, str) and path.startswith("text:"):
                m = re.search(path[5:], resp["text"] or "")
                variables[var] = m.group(1) if m and m.groups() else (m.group(0) if m else None)
            elif isinstance(path, str) and path.startswith("元素:"):
                el = (resp.get("json") or {}).get("elements", {}).get(path[3:]) or {}
                variables[var] = el.get("text") or el.get("value")
            elif isinstance(path, str) and path.startswith("captcha:"):
                # captcha:data.xxx —— 从响应里取出 base64 图片，直接 OCR 成文本。
                # 图形验证码的登录/注册流程靠它就能全自动，不用把图存到磁盘、
                # 也不用人工读一次。
                from . import captcha as captcha_mod

                b64 = deep_get(resp.get("json"), path.split(":", 1)[1])
                if not b64:
                    variables[var] = None
                else:
                    if isinstance(b64, str) and "," in b64:
                        b64 = b64.split(",", 1)[1]
                    try:
                        import base64 as _b64
                    except ImportError:
                        import base64 as _b64  # type: ignore
                    try:
                        variables[var] = captcha_mod.recognize(
                            _b64.b64decode(b64), expect_len=step.get("验证码长度"))
                    except Exception as e:
                        variables[var] = None
                        extract_fail = True
                        failures.append("%s → 验证码识别失败：%s: %s"
                                        % (name, type(e).__name__, e))
            else:
                variables[var] = deep_get(resp["json"], path)

        checks, step_ok = [], not extract_fail
        if observation_failure:
            checks.append({"assertion": {"类型": "观测等待", "来源": observation_wait.get("来源")},
                           "passed": False, "actual": observation_failure,
                           "blocked": observation_blocked})
            step_ok = False
            failures.append("%s → %s" % (name, observation_failure))
        if resp.get("virtual_fault_blocked"):
            detail = resp.get("virtual_fault_error") or "虚拟故障未触发"
            checks.append({"assertion": {"类型": "故障"}, "passed": False, "actual": detail})
            step_ok = False
            failures.append("%s → %s" % (name, detail))
        # 轮询没成立：这是本步骤的根因，排在最前面，别让下游断言抢了位置
        if poll_failure:
            checks.append({"assertion": {"类型": "轮询", "直到": (poll or {}).get("直到")},
                           "passed": False, "actual": poll_failure})
            step_ok = False
            failures.append("%s → %s" % (name, poll_failure))

        # 通道自带的前置检查（如"导航返回 404，页面根本没打开"）。
        # 放在用例自己的断言之前，让报告第一眼看到的是根因，而不是一堆下游连锁失败。
        for pc in resp.get("precheck") or []:
            checks.append(pc)
            if not pc.get("passed"):
                step_ok = False
                failures.append("%s → %s" % (name, pc.get("actual", "")))

        for a in step.get("断言") or []:
            try:
                if isinstance(a, dict) and a.get("类型") == "响应契约":
                    ok, detail, evidence = contract_mod.validate_response(
                        a, resp, variables["__contract_registry"])
                    check = {"assertion": a, "passed": ok, "actual": detail, "contract": evidence}
                else:
                    ok, detail = judge(a, resp, variables)
                    check = {"assertion": a, "passed": ok, "actual": detail}
            except (AssertError, contract_mod.ContractSpecError, KeyError) as e:
                ok, detail = False, "规格错误：%s" % e
                check = {"assertion": a, "passed": ok, "actual": detail}
            checks.append(check)
            if not ok:
                step_ok = False
                failures.append("%s → %s" % (name, detail))

        # 只有「通道级失败」才算环境阻断：连不上、DNS 解析不了、导航没打开。
        # 页面动作失败（元素找不到、断言不符）是行为问题，要算缺陷。
        # 这两者混算，报告要么一片红（把环境问题当缺陷），要么一片绿（把缺陷当环境）。
        if not step_ok:
            channel_failed = resp["status"] == 0 or resp.get("ui_nav_failed") or observation_blocked
            if not channel_failed:
                net_error_only = False

        shot = None
        if not step_ok and ui_session is not None and resp.get("ui") \
                and ui_session.snapshots_dir:
            safe = _safe_name(name)[:40]
            diag_before = len(ui_session.diagnostics)
            shot = ui_session.screenshot(
                os.path.join(ui_session.snapshots_dir, "%s--%s.png" % (case_id or "case", safe))
            )
            if len(ui_session.diagnostics) > diag_before:
                # 截图发生在快照之后，它的失败信息得补回本步骤 —— 否则
                # 「报告里截图为什么是空白」这件最需要解释的事永远进不了结果。
                resp["diagnostics"] = ((resp.get("diagnostics") or [])
                                       + ui_session.diagnostics[diag_before:])

        step_results.append({
            "name": name,
            "method": resp["method"],
            "url": resp["url"],
            "status": resp["status"],
            "elapsed_ms": resp["elapsed_ms"],
            "passed": step_ok,
            "net_error": resp["status"] == 0,
            "ui": bool(resp.get("ui")),
            "checks": checks,
            "response_excerpt": (resp["text"] or "")[:1500],
            "text_collected": resp.get("text_collected", True),
            "diagnostics": resp.get("diagnostics") or [],
            "screenshot": shot,
            "poll": poll_info,
            "observation_wait": observation_wait_info,
            # 下载产物：留住名字/路径/字节数，报告里能直接说清「下到了什么」
            "downloads": resp.get("downloads") or [],
            "concurrency": resp.get("concurrency") or {},
            # 只落盘脱敏后的摘要，原始记录只存在于断言执行期间。
            "observability": {key: observe_mod.public_result(value) for key, value in
                               (resp.get("observations") or {}).items()},
            "virtual": resp.get("virtual") or {},
        })

        if not step_ok and stop_on_fail:
            break
    return step_results, failures, (net_error_only and bool(failures))


def _safe_name(s):
    return re.sub(r"[^\w\u4e00-\u9fa5.-]", "_", s or "")


def _run_case(client, base, case, spec_vars, auth_headers, ui_session=None, ui_base=None):
    cid = case["id"]
    if ui_session is not None:
        # 快照要抓的选择器在用例级一次性确定：断言用到的也算，否则断言查不到元素会误判
        ui_session.selectors = ui_mod.collect_selectors(case)
        # 只有出现文本类断言才去读整页 innerText，其余情况省掉这次开销
        ui_session.need_text = ui_mod.needs_body_text(case)
    variables = dict(spec_vars)
    variables.update(case.get("vars") or {})
    t0 = time.time()
    attempts = max(0, int(case.get("重试", 0))) + 1
    last = None

    for attempt in range(attempts):
        variables = dict(spec_vars)
        variables.update(case.get("vars") or {})
        stage = []
        failures, net_only = [], False

        # 用例前置：造数据 / 取 ID
        if case.get("setup"):
            s_res, s_fail, s_net = _run_steps(
                client, base, case["setup"], variables, auth_headers, stop_on_fail=True,
                ui_session=ui_session, case_id=cid, ui_base=ui_base,
            )
            stage.extend(s_res)
            failures.extend(s_fail)
            net_only = s_net
            if failures:
                if case.get("teardown"):
                    _run_steps(client, base, case["teardown"], variables, auth_headers, False,
                               ui_session=ui_session, case_id=cid, ui_base=ui_base)
                return {
                    "id": cid, "module": case.get("模块", ""), "title": case.get("用例标题", ""),
                    "priority": case.get("优先级", ""), "type": case.get("用例类型", ""),
                    "status": "blocked" if net_only else "failed",
                    "flaky": False, "duration_ms": int((time.time() - t0) * 1000),
                    "reason": "前置步骤失败：" + "；".join(failures),
                    "basis": case.get("依据", ""), "defect_level": case.get("缺陷等级", ""),
                    "steps": stage, "retried": attempt > 0,
                    "data": case.get("_data"),
                }

        steps = case.get("steps") or []
        stop_on_fail = case.get("失败即止", True)
        step_results, fails, net_only2 = _run_steps(
            client, base, steps, variables, auth_headers, stop_on_fail,
            ui_session=ui_session, case_id=cid, ui_base=ui_base,
        )
        stage.extend(step_results)
        failures.extend(fails)
        net_only = net_only2 if fails else False

        # 后置清理：无论成败都跑，避免脏数据堆积
        if case.get("teardown"):
            t_res, t_fail, _ = _run_steps(
                client, base, case["teardown"], variables, auth_headers, False,
                ui_session=ui_session, case_id=cid, ui_base=ui_base,
            )
            stage.extend(t_res)
            if t_fail:
                failures.append("清理步骤：" + "；".join(t_fail))

        n_ok = sum(1 for s in stage for c in s["checks"] if c["passed"])
        n_all = sum(len(s["checks"]) for s in stage)
        case_status = "passed" if not failures else ("blocked" if net_only else "failed")
        if case_status == "blocked":
            # 阻断 = 根本没测成，这些断言不该计入通过率分子分母
            n_ok, n_all = 0, 0
        last = {
            "id": cid, "module": case.get("模块", ""), "title": case.get("用例标题", ""),
            "priority": case.get("优先级", ""), "type": case.get("用例类型", ""),
            "status": case_status,
            "flaky": False, "duration_ms": int((time.time() - t0) * 1000),
            "reason": "" if not failures else "；".join(failures),
            "basis": case.get("依据", ""), "defect_level": case.get("缺陷等级", ""),
            "steps": stage, "assertions": {"passed": n_ok, "total": n_all},
            "retried": attempt > 0,
            "data": case.get("_data"),
        }
        if not failures:
            if attempt > 0:
                last["flaky"] = True
                last["reason"] = "首次执行失败、重试后通过 —— 行为不稳定，优先排查"
            break
        if last["status"] == "blocked":
            break
    return last


def _needs_ui(spec, auth_cfg):
    """这个规格到底用不用得上浏览器。用不上就别启动，省几秒启动时间。"""
    t = (auth_cfg or {}).get("type") or ""
    t = auth_mod.ALIASES.get(t, t)
    if t == "browser_login":
        return True
    for c in spec.get("cases") or []:
        for key in ("steps", "setup", "teardown"):
            for st in c.get(key) or []:
                if isinstance(st, dict) and (st.get("ui") or st.get("动作")):
                    return True
        for a in c.get("断言") or []:
            if isinstance(a, dict) and a.get("类型") in ("页面", "URL", "控制台", "网络"):
                return True
    return False


def _open_ui(spec, ctx, spec_dir, out_dir):
    """按需创建浏览器会话。返回 (session, 不可用原因)。"""
    if not _needs_ui(spec, ctx["auth"]):
        return None, ""
    ok, why = ui_mod.is_available()
    if not ok:
        ctx["auth"]["_ui_unavailable"] = why
        return None, why
    from . import ui_auth

    ui_cfg = ctx.get("ui") or {}
    state = ""
    try:
        state, _note = ui_auth.storage_state_path(ctx["auth"], spec_dir)
    except Exception:
        state = ""      # 交给 authenticate 报更具体的错（那里知道有没有 login_steps）

    session = ui_mod.UiSession(
        headless=ui_cfg.get("headless", True),
        timeout=ui_cfg.get("timeout", 15000),
        prefer=ui_cfg.get("browser") or {},
        storage_state=state or None,
        ignore_https_errors=not ctx["verify_tls"],
        viewport=ui_cfg.get("viewport"),
        slow_mo=ui_cfg.get("slow_mo", 0),
        network_limit=ui_cfg.get("network_limit"),
        download_dir=os.path.join(out_dir, "下载"),
    )
    try:
        session.start()
    except Exception as e:
        ctx["auth"]["_ui_unavailable"] = str(e)
        return None, str(e)
    session.snapshots_dir = os.path.join(out_dir, "截图")
    return session, ""


def _start_virtual_services(spec, ctx):
    """启动规格声明的 loopback 虚拟服务，并只暴露显式 base_url 变量。"""
    declarations = spec.get("virtual_services") or []
    if not declarations:
        return {}
    safety_mod.require_isolated(ctx, "虚拟依赖")
    services = {}
    try:
        for declaration in declarations:
            name = declaration["name"]
            service = virtual_mod.VirtualService(name, declaration["routes"])
            service.start()
            services[name] = service
        return services
    except Exception:
        for service in services.values():
            service.stop()
        raise


def _install_step_fault(step, variables):
    """安装仅覆盖当前 HTTP 步骤的故障规则，返回可供落盘的证据上下文。"""
    fault = step.get("故障")
    if not fault:
        return None
    safety_mod.require_isolated(variables["__ctx"], "虚拟依赖")
    service = (variables.get("__virtual_services") or {}).get(fault["服务"])
    if service is None:
        raise virtual_mod.VirtualError("未启动虚拟服务：%s" % fault["服务"])
    start = len(service.hits)
    fault_id = service.install_fault(fault)
    return {"service": service, "fault_id": fault_id, "hit_start": start,
            "rule": {k: v for k, v in fault.items() if k != "response"}}


def _finish_step_fault(context, resp):
    if context is None:
        return
    service = context["service"]
    try:
        hits = service.evidence(context["hit_start"])
        matched = [hit for hit in hits if hit.get("fault_id") == context["fault_id"]]
        resp["virtual"] = {
            "service": service.name,
            "base_url": service.base_url,
            "scope": "current_step",
            "rule": context["rule"],
            "fault_id": context["fault_id"],
            "hits": hits,
            "matched_hits": matched,
            "triggered": bool(matched),
        }
        if not matched:
            resp["virtual_fault_blocked"] = True
            resp["virtual_fault_error"] = "虚拟故障未触发：服务 %s 未收到匹配 %s %s 的请求" % (
                service.name, context["rule"]["match"]["method"],
                context["rule"]["match"]["path"])
    finally:
        service.remove_fault(context["fault_id"])


def run_spec(
    spec_path,
    out_dir=None,
    only=None,
    env_name=None,
    env_file=None,
    retry_override=None,
    quiet=False,
    fail_fast=False,
    junit_path=None,
):
    """管理虚拟服务完整生命周期；无论执行、认证或报告发生何种异常都停止服务。"""
    spec_path = os.path.abspath(spec_path)
    spec = load_spec(spec_path)
    ctx = env_mod.resolve(spec, os.path.dirname(spec_path), env_name, env_file)
    services = {}
    virtual_error = ""
    try:
        services = _start_virtual_services(spec, ctx)
    except (safety_mod.SafetyError, virtual_mod.VirtualError, KeyError, TypeError) as e:
        virtual_error = str(e)
    try:
        return _run_spec(
            spec_path, out_dir=out_dir, only=only, env_name=env_name, env_file=env_file,
            retry_override=retry_override, quiet=quiet, fail_fast=fail_fast, junit_path=junit_path,
            virtual_services=services, virtual_error=virtual_error,
        )
    finally:
        for service in services.values():
            service.stop()


def _run_spec(
    spec_path,
    out_dir=None,
    only=None,
    env_name=None,
    env_file=None,
    retry_override=None,
    quiet=False,
    fail_fast=False,
    junit_path=None,
    virtual_services=None,
    virtual_error="",
):
    started = time.time()
    spec_path = os.path.abspath(spec_path)
    spec_dir = os.path.dirname(spec_path)
    spec = load_spec(spec_path)
    try:
        contract_registry = contract_mod.build_registry(spec.get("contract"), spec_dir)
    except contract_mod.ContractSpecError as e:
        raise ValueError("契约规格错误：%s" % e)
    ctx = env_mod.resolve(spec, spec_dir, env_name, env_file)
    ctx["vars"] = dict(ctx.get("vars") or {})
    ctx["vars"]["__contract_registry"] = contract_registry
    ctx["vars"]["__ctx"] = ctx
    ctx["vars"]["__run_id"] = uuid.uuid4().hex[:12]
    ctx["vars"]["__observability"] = spec.get("observability") or {}
    ctx["vars"]["__virtual_services"] = dict(virtual_services or {})
    for name, service in (virtual_services or {}).items():
        ctx["vars"]["virtual.%s.base_url" % name] = service.base_url

    client = HttpClient(
        timeout=ctx["timeout"], verify_tls=ctx["verify_tls"], default_headers=ctx["headers"]
    )
    out_dir = out_dir or os.path.join(spec_dir, "测试结果")
    # 下载产物的默认落点（HTTP 与 UI 共用同一个目录，报告里只认这一处）
    client.download_dir = os.path.join(out_dir, "下载")

    # ---------------- 浏览器通道（可选）
    # 只有规格里真的用到 UI 才启动浏览器：纯接口规格不该被 Playwright 拖慢或拖垮。
    ui_session, ui_problem = _open_ui(spec, ctx, spec_dir, out_dir)
    client.ui_problem = ui_problem

    auth_headers, auth_error, auth_info = {}, None, {}
    if virtual_error:
        auth_error = "虚拟依赖被阻断：%s" % virtual_error
    try:
        if auth_error:
            raise auth_mod.AuthError(auth_error)
        auth_headers, _cookies, auth_info = auth_mod.authenticate(
            ctx["auth"], ctx["base"], spec_dir, env_mod.env_map(ctx), client, ctx["vars"],
            session=ui_session, origin=ctx["base_url"],
        )
    except auth_mod.AuthError as e:
        auth_error = str(e)
    except Exception as e:
        auth_error = "%s: %s" % (type(e).__name__, e)

    # 浏览器登录顺手把 cookie 同步给 HTTP 客户端，两条通道登录态才一致
    if ui_session is not None and not auth_error:
        try:
            for c in (ui_session.storage_state() or {}).get("cookies", []):
                client.cookies[c["name"]] = c["value"]
        except Exception:
            pass

    cases = _expand_cases(spec["cases"])
    if only:
        cases = [c for c in cases if c["id"].startswith(only)]

    results = []
    status_by_id = {}
    # fail-fast 提前中断时，算出来的覆盖度不是覆盖度，只是「跑到哪儿为止的覆盖度」
    interrupted = False

    if not quiet:
        print("规格：%s  v%s" % (spec.get("name", os.path.basename(spec_path)), spec.get("spec_version", "?")))
        print("环境：%s  %s" % (ctx["env_name"], ctx["base"]))
        print("认证：%s  %s" % (
            auth_info.get("type", ctx["auth"].get("type", "none")),
            "凭据已就绪" if not auth_error else "凭据获取失败",
        ))
        if ui_session is not None:
            print("浏览器：%s" % (ui_session.browser_note or "已启动"))
        elif ui_problem:
            print("浏览器：不可用（%s）" % ui_problem[:120])
        print("用例：%d 条%s\n" % (len(cases), "（筛选 %s*）" % only if only else ""))

    for case in cases:
        cid = case["id"]
        is_manual = case.get("自动化") == "manual" or case.get("手动")

        if is_manual:
            results.append({
                "id": cid, "module": case.get("模块", ""), "title": case.get("用例标题", ""),
                "priority": case.get("优先级", ""), "type": case.get("用例类型", ""),
                "status": "manual", "flaky": False, "duration_ms": 0,
                "reason": case.get("阻塞原因", "") or case.get("依据", ""),
                "basis": case.get("依据", ""), "defect_level": case.get("缺陷等级", ""),
                "steps": [], "assertions": {"passed": 0, "total": 0},
                "data": case.get("_data"),
            })
            status_by_id[cid] = "manual"
            if not quiet:
                print("[手动] %s %s" % (cid, case.get("用例标题", "")))
            continue

        # 依赖用例没通过就不跑，避免级联误报
        deps = case.get("依赖") or []
        bad_deps = _bad_deps(deps, status_by_id)
        if bad_deps:
            results.append({
                "id": cid, "module": case.get("模块", ""), "title": case.get("用例标题", ""),
                "priority": case.get("优先级", ""), "type": case.get("用例类型", ""),
                "status": "skipped", "flaky": False, "duration_ms": 0,
                "reason": "依赖的用例未通过：%s" % "、".join(bad_deps),
                "basis": case.get("依据", ""), "defect_level": "", "steps": [],
                "assertions": {"passed": 0, "total": 0},
                "data": case.get("_data"),
            })
            status_by_id[cid] = "skipped"
            if not quiet:
                print("[跳过] %s 依赖未通过 %s" % (cid, "、".join(bad_deps)))
            continue

        if auth_error:
            results.append({
                "id": cid, "module": case.get("模块", ""), "title": case.get("用例标题", ""),
                "priority": case.get("优先级", ""), "type": case.get("用例类型", ""),
                "status": "blocked", "flaky": False, "duration_ms": 0,
                "reason": "凭据未就绪，未执行：%s" % auth_error,
                "basis": case.get("依据", ""), "defect_level": "", "steps": [],
                "assertions": {"passed": 0, "total": 0},
                "data": case.get("_data"),
            })
            status_by_id[cid] = "blocked"
            continue

        case_for_run = dict(case)
        if retry_override is not None:
            case_for_run["重试"] = retry_override
        r = _run_case(client, ctx["base"], case_for_run, ctx["vars"], auth_headers,
                      ui_session=ui_session, ui_base=ctx["base_url"])
        results.append(r)
        status_by_id[cid] = r["status"]

        if not quiet:
            mark = {"passed": "通过", "failed": "失败", "blocked": "阻断"}[r["status"]]
            asrt = r.get("assertions") or {}
            print("[%s] %s %s  断言 %s/%s%s" % (
                mark, cid, r["title"], asrt.get("passed", 0), asrt.get("total", 0),
                "  (flaky)" if r.get("flaky") else "",
            ))
            if r["status"] != "passed" and r["reason"]:
                print("       %s" % r["reason"][:220])

        if fail_fast and r["status"] in ("failed", "blocked"):
            interrupted = True
            break

    # ---------------- 需求点回填
    # 结果条目有四处构造点（manual / skipped / blocked / 正常执行），统一在这里按 id 合并，
    # 避免四处各写一遍、漏一处就静默少算一条需求的覆盖。
    reqs_by_id = {c["id"]: c.get("需求点") or [] for c in cases}
    for r in results:
        r["requirements"] = reqs_by_id.get(r["id"], [])

    coverage = build_coverage(spec, results, selected=only or "", interrupted=interrupted)

    # ---------------- 汇总
    auto = [r for r in results if r["status"] in ("passed", "failed", "blocked")]
    passed = [r for r in results if r["status"] == "passed"]
    failed = [r for r in results if r["status"] == "failed"]
    blocked = [r for r in results if r["status"] == "blocked"]
    skipped = [r for r in results if r["status"] == "skipped"]
    manual = [r for r in results if r["status"] == "manual"]
    flaky = [r for r in results if r.get("flaky")]
    a_ok = sum((r.get("assertions") or {}).get("passed", 0) for r in results)
    a_all = sum((r.get("assertions") or {}).get("total", 0) for r in results)

    # 口径（不要合并成一个数）：
    #   已判定通过率 = 通过 /（通过 + 失败）——只算真正得出产品结论的用例
    #   自动执行完成率 =（通过 + 失败）/ 总数——反映有多少用例跑到了结论
    # 阻断是「根本没测成」，放进任何分子分母都会把环境问题算成产品质量。
    decided = len(passed) + len(failed)

    summary = {
        "total": len(results),
        "auto": len(auto),
        "passed": len(passed),
        "failed": len(failed),
        "blocked": len(blocked),
        "skipped": len(skipped),
        "manual": len(manual),
        "flaky": len(flaky),
        "decided": decided,
        "pass_rate": round(len(passed) * 100.0 / decided, 1) if decided else 0.0,
        "completion_rate": round(decided * 100.0 / len(results), 1) if results else 0.0,
        # 旧口径（分母含阻断），只保留给外部消费方对账；报告不再使用它。
        "pass_rate_including_blocked": round(len(passed) * 100.0 / len(auto), 1) if auto else 0.0,
        "assertions_total": a_all,
        "assertions_passed": a_ok,
        # 数据驱动展开出来的行数：为 0 说明这份规格没用数据，报告里就不多这一行
        "data_rows": len([r for r in results if r.get("data")]),
        # 覆盖度摘要：compare 时能看出「上轮没覆盖的需求这轮补上了」还是「用例变少了」
        "coverage_total": coverage["total"],
        "coverage_verified": coverage["verified"],
        "coverage_uncovered": len(coverage["uncovered"]),
    }

    run_id = ctx["vars"]["__run_id"]
    result = {
        "schema": RESULT_SCHEMA,
        "tool": {"name": TOOL, "version": VERSION},
        "run": {
            "id": run_id,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": _now(),
            "duration_ms": int((time.time() - started) * 1000),
            "spec_path": spec_path,
            "spec_name": spec.get("name", ""),
            "spec_version": spec.get("spec_version", ""),
            "filter": only or "",
        },
        "target": {
            "env": ctx["env_name"],
            "base": ctx["base"],
            "auth_type": auth_info.get("type", ctx["auth"].get("type", "none")),
            "credential_ok": not auth_error,
            "credential_error": auth_error or "",
            "credential_source": auth_info.get("source", ""),
            "ui": {
                "enabled": ui_session is not None,
                "browser": (ui_session.browser_note if ui_session else ""),
                "problem": ui_problem or "",
            },
        },
        "summary": summary,
        "coverage": coverage,
        "cases": results,
        "defects": [
            {
                "case": r["id"], "module": r["module"], "title": r["title"],
                "priority": r["priority"], "level": (r.get("defect_level") or "未分级").split("｜")[0],
                "level_note": r.get("defect_level", ""), "reason": r["reason"],
                "basis": r.get("basis", ""),
            }
            for r in failed
        ],
    }

    out_dir = out_dir or os.path.join(spec_dir, "测试结果")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S")
    result_path = os.path.join(out_dir, "result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    if ui_session is not None:
        ui_session.stop()

    from . import report as report_mod
    md_path, html_path = report_mod.write_reports(result, out_dir, stamp)

    # JUnit 给 CI 读（Jenkins/GitLab 直接显示用例级结果），md/html 给人读。
    # 默认落在输出目录，--junit 可指到别处；写失败不该让整轮结果作废。
    from . import junit as junit_mod
    junit_out = junit_path or os.path.join(out_dir, "junit.xml")
    try:
        junit_out = junit_mod.write_junit(result, junit_out)
    except OSError as e:
        junit_out = ""
        if not quiet:
            print("!! JUnit 写入失败：%s（其余结果不受影响）" % e)

    if not quiet:
        print("\n自动执行 %d 条：通过 %d，失败 %d，阻断 %d，跳过 %d；手动 %d 条" % (
            len(auto), len(passed), len(failed), len(blocked), len(skipped), len(manual)))
        if summary["data_rows"]:
            print("数据驱动：%d 条用例展开为 %d 行" % (
                len({r["data"]["base_id"] for r in results if r.get("data")}),
                summary["data_rows"]))
        cov_line = summarize_coverage(coverage)
        if cov_line:
            print(cov_line + ("　（本次为筛选/提前中断执行，覆盖度只代表所选范围）"
                              if coverage["partial_run"] else ""))
            if coverage["uncovered"]:
                print("      未覆盖需求点：%s" % "、".join(coverage["uncovered"]))
        if auth_error:
            print("!! 凭据问题：%s" % auth_error)
        print("结果：%s" % result_path)
        print("报告：%s" % md_path)
        if html_path:
            print("      %s" % html_path)
        if junit_out:
            print("JUnit：%s" % junit_out)

    result["_paths"] = {"result": result_path, "md": md_path, "html": html_path,
                        "junit": junit_out}
    return result
