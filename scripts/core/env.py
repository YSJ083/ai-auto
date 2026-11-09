"""环境档 —— 把「跑在哪」和「测什么」分开。

同一份规格要能跑 dev / test / uat，只需换 --env，不改用例。
凭据不写进规格：环境档只存变量名，真实值来自系统环境变量或本地 .env 文件。
"""

import json
import os

DEFAULT_PROFILE_KEYS = ("base_url", "api_prefix", "vars", "headers", "timeout",
                        "verify_tls", "auth", "ui", "autotest")


def _read_dotenv(path):
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_profiles(spec, spec_dir, env_file=None):
    """返回 (profiles dict, 来源说明)。

    优先外部文件 --env-file / 规格同目录 environments.json，其次规格内联 environments。
    """
    candidates = []
    if env_file:
        candidates.append(env_file if os.path.isabs(env_file) else os.path.join(spec_dir, env_file))
    candidates.append(os.path.join(spec_dir, "environments.json"))
    for p in candidates:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("profiles", data), p
    if spec.get("environments"):
        return spec["environments"], "规格内联"
    return {}, ""


def resolve(spec, spec_dir, env_name=None, env_file=None, dotenv=None):
    """合并出最终执行上下文。

    优先级：环境档 > 规格 defaults > 规格顶层字段。
    """
    profiles, source = load_profiles(spec, spec_dir, env_file)
    name = env_name or spec.get("env") or "default"
    profile = {}
    if profiles:
        if name not in profiles and "default" in profiles:
            name = "default"
        profile = profiles.get(name) or {}
        if not profile and len(profiles) == 1:
            name, profile = list(profiles.items())[0]

    ctx = {
        "env_name": name,
        "env_source": source,
        "base_url": profile.get("base_url") or spec.get("base_url") or "",
        "api_prefix": profile.get("api_prefix", spec.get("api_prefix", "")),
        "vars": dict(spec.get("vars") or {}),
        "headers": dict(profile.get("headers") or {}),
        "timeout": profile.get("timeout") or spec.get("timeout") or 15,
        "verify_tls": profile.get("verify_tls", spec.get("verify_tls", True)),
        "auth": profile.get("auth") or spec.get("auth") or {},
        "ui": profile.get("ui") or spec.get("ui") or {},
        "autotest": profile.get("autotest") or spec.get("autotest") or {},
        "dotenv": {},
    }
    ctx["vars"].update(profile.get("vars") or {})

    # 密钥：本地 .env 优先于系统环境变量（便于项目自带一套）
    dot = dotenv or os.path.join(spec_dir, ".env")
    ctx["dotenv"] = _read_dotenv(dot)
    ctx["dotenv_source"] = dot if os.path.exists(dot) else ""

    base = (ctx["base_url"] or "").rstrip("/") + (ctx["api_prefix"] or "")
    if not base:
        raise ValueError("没有可用地址：规格与环境档都没有给出 base_url")
    ctx["base"] = base
    return ctx


def env_map(ctx):
    """给认证模块用的键值表：.env + 系统环境变量。"""
    m = dict(os.environ)
    m.update(ctx.get("dotenv") or {})
    return m
