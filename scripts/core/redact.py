# -*- coding: utf-8 -*-
"""敏感信息脱敏：结果文件、报告、汇总层共用的唯一实现。

为什么单独成模块：结果要经过「执行 → 汇总 → 报告」三道落盘，
任何一道漏脱敏，明文都会留在磁盘上。规则只写一份，避免两处漂移。

与 testcase-designer/scripts/run_agent_eval.py 的 redact 保持同一套规则；
改这里必须同步改那边，并在两侧测试里各跑一遍同样的样本。
"""

import re

SENSITIVE_KEY = re.compile(
    r"password|passwd|token|secret|api[_-]?key|authorization|cookie|session|credential",
    re.I,
)
SENSITIVE_VALUE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*"
    r"|((?:api[_-]?key|password|passwd|token|secret|cookie)\s*[:=]\s*)[^\s,;]+"
)

_enabled = True
_extra_keys = []


def configure(enabled=True, extra_keys=None):
    """enabled=False 仅用于本地调试；extra_keys 追加业务敏感字段名。"""
    global _enabled, _extra_keys
    _enabled = bool(enabled)
    _extra_keys = [k for k in (extra_keys or []) if k]


def enabled():
    return _enabled


def is_sensitive_key(key):
    if not _enabled:
        return False
    text = str(key or "")
    if SENSITIVE_KEY.search(text):
        return True
    lowered = text.lower()
    return any(k.lower() in lowered for k in _extra_keys)


def redact(value, key=""):
    if not _enabled:
        return value
    if is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_VALUE.sub(
            lambda m: (m.group(1) or m.group(2) or "") + "[REDACTED]", value
        )
    return value
