"""新型高影响测试能力的环境安全策略。"""

import urllib.parse


class SafetyError(Exception):
    pass


def policy(ctx):
    return dict(ctx.get("autotest") or {})


def _host(url):
    return (urllib.parse.urlparse(url).hostname or "").lower()


def require_isolated(ctx, capability, url=None):
    p = policy(ctx)
    if p.get("mode") != "isolated":
        raise SafetyError("%s 只允许在 autotest.mode=isolated 的隔离环境执行" % capability)
    allowed = set(p.get("allow_hosts") or [])
    target = _host(url or ctx.get("base_url") or "")
    if not target or target not in allowed:
        raise SafetyError("%s 的目标主机 %s 不在 autotest.allow_hosts 白名单" % (capability, target or "(空)"))
    key = {"并发写": "allow_concurrency_writes", "虚拟依赖": "allow_virtualization", "观测网关": "allow_observation_gateway"}.get(capability)
    if key and not p.get(key):
        raise SafetyError("环境未显式允许%s（缺少 autotest.%s=true）" % (capability, key))


def is_loopback_url(url):
    return _host(url) in ("127.0.0.1", "localhost", "::1")
