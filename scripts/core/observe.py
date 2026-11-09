"""测试专用只读观测网关客户端。

网关须由项目部署并仅暴露预声明来源；本模块绝不直连 DB、MQ 或 Trace
后端，也不接收 SQL、MQ 消费命令或 TraceQL。
"""
import json
import urllib.parse
import urllib.request

from . import redact as redact_mod


class ObservationUnavailable(Exception):
    """观测通道未提供可判定的完整只读结果，应稳定归为 blocked。"""


def _unavailable(message):
    raise ObservationUnavailable(message)


def _safe_summary(body, source_name, kind, correlation):
    """仅保留可落盘的脱敏、限长摘要；原始记录只驻留在本次断言内存中。"""
    raw_records = body.get("records")
    if not isinstance(raw_records, list):
        _unavailable("观测网关返回 records 必须是数组")
    sample = redact_mod.redact(raw_records[:3])
    return {
        "source": source_name,
        "kind": kind,
        "correlation": dict(correlation),
        "record_count": len(raw_records),
        "records_sample": sample,
        "read_only": True,
        "truncated": False,
    }


def collect(cfg, source_name, correlation):
    """经受限 HTTP 网关采集预声明来源。

    请求和响应均强制校验 run_id/case_id/step_id；未回显、截断、非只读、来源
    或类型不一致均不是规格错误，而是观测通道不可用。
    """
    if not isinstance(cfg, dict):
        _unavailable("observability 配置必须是对象")
    source = (cfg.get("sources") or {}).get(source_name)
    if not isinstance(source, dict):
        _unavailable("观测来源未声明：%s" % source_name)
    kind = source.get("kind")
    if kind not in ("DB", "MQ", "Trace"):
        _unavailable("观测来源 %s 的 kind 非法" % source_name)
    gateway = cfg.get("gateway") or {}
    base = str(gateway.get("base_url") or "").rstrip("/")
    if not base:
        _unavailable("未配置 observability.gateway.base_url")
    if not all(isinstance(correlation.get(k), str) and correlation[k]
               for k in ("run_id", "case_id", "step_id")):
        _unavailable("观测查询必须携带非空 run_id/case_id/step_id")

    query = urllib.parse.urlencode({"source": source_name, **correlation})
    req = urllib.request.Request(
        base + "/query?" + query,
        headers={"Accept": "application/json", "X-Autotest-Readonly": "true"},
        method="GET",
    )
    timeout_ms = gateway.get("timeout_ms", 3000)
    try:
        with urllib.request.urlopen(req, timeout=(timeout_ms / 1000.0)) as response:
            if response.getcode() != 200:
                _unavailable("观测网关返回 HTTP %s" % response.getcode())
            body = json.loads(response.read().decode("utf-8"))
    except ObservationUnavailable:
        raise
    except Exception as e:
        _unavailable("观测网关不可用：%s" % e)
    if not isinstance(body, dict):
        _unavailable("观测网关返回必须是 JSON 对象")
    if body.get("read_only") is not True:
        _unavailable("观测网关未确认 read_only=true")
    if body.get("truncated") is True:
        _unavailable("观测网关结果已截断，拒绝给出不完整结论")
    if body.get("source") != source_name or body.get("kind") != kind:
        _unavailable("观测来源或类型与声明不匹配")
    returned = body.get("correlation")
    if not isinstance(returned, dict) or any(returned.get(k) != correlation[k]
                                               for k in correlation):
        _unavailable("观测网关未原样回显 run_id/case_id/step_id")

    summary = _safe_summary(body, source_name, kind, correlation)
    # 原始记录只供本轮断言读取。runner 落盘时会删除该私有字段。
    return {"records": summary["records_sample"], "_records": body["records"],
            "summary": summary}


def public_result(observation):
    """删除仅供断言使用的原始记录，形成可安全写入 result.json 的证据。"""
    return {key: value for key, value in (observation or {}).items() if key != "_records"}
