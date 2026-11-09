"""认证适配器 —— 把「怎么拿到身份」从用例里剥出来。

被测系统的登录方式千差万别，但执行器只关心一件事：**后续请求要带什么头/什么 Cookie**。
所以这里把认证收敛成 7 种可插拔类型，用例本身完全不用知道登录怎么做的。

支持：
  none          不需要身份（或纯匿名场景）
  header        固定请求头，值可来自文件 / 环境变量 / 字面量（v1 的 token_from 走这里）
  bearer        Authorization: Bearer <token>
  cookie        直接给 Cookie 串，或从文件读
  form_login    HTTP 表单/JSON 登录，自动提取 token 或会话 Cookie（支持前置 CSRF 步骤）
  command       调外部命令拿凭据（stdout 输出 token 或 {"token": "..."}）
  browser_login 原生浏览器登录：复用人登录一次存下的 storageState，或按 login_steps 填表登录。
                详见 references/auth-adapters.md 与 core/ui_auth.py 的三级策略
                （L1 storageState / L2 login_steps / L3 验证码不做进主流程）。

所有类型都支持 probe：认证完成后打一个探活接口，凭据失效时立刻报「环境阻断」，
而不是让几十条用例集体失败、把报告变成一片红。
"""

import json
import os
import subprocess

from .asserts import deep_get, fill, judge

AUTH_TYPES = ["none", "header", "bearer", "cookie", "form_login", "command", "browser_login"]

# 老规格里的写法，映射到统一类型（v1 的 seed_token 就是「固定头 + 文件凭据」）
ALIASES = {
    "seed_token": "header",
    "token": "header",
    "token_file": "header",
    "api_key": "header",
    "jwt": "bearer",
    "session": "cookie",
    "login": "form_login",
    "script": "command",
    "playwright": "browser_login",
}


class AuthError(Exception):
    """凭据获取失败 —— 属于环境问题，不是产品缺陷。"""


def _read_text(path, base_dir):
    p = path if os.path.isabs(path) else os.path.join(base_dir, path)
    if not os.path.exists(p):
        raise AuthError("凭据文件不存在：%s" % p)
    with open(p, encoding="utf-8") as f:
        return f.read().strip()


def _resolve_secret(cfg, keys, base_dir, env_map):
    """按 字面量 > 环境变量 > 文件 的顺序取值。"""
    for k in keys:
        if k in cfg and cfg[k] not in (None, ""):
            return cfg[k], "%s(字面量)" % k
    for k in ("token_env", "cookie_env", "value_env"):
        if cfg.get(k):
            name = cfg[k]
            if name in env_map:
                return env_map[name], "env:%s" % name
            if os.environ.get(name):
                return os.environ[name], "os-env:%s" % name
            raise AuthError("环境变量 %s 未设置" % name)
    for k in ("token_from", "cookie_from", "value_from", "file"):
        if cfg.get(k):
            return _read_text(cfg[k], base_dir), "file:%s" % cfg[k]
    return None, None


def _browser_login(cfg, base, spec_dir, env_map, variables, session, headers, cookies, info,
                   origin=""):
    """原生浏览器登录。三级策略见 core/ui_auth.py 顶部注释。"""
    from . import ui_auth

    if session is None:
        reason = cfg.get("_ui_unavailable") or "浏览器会话不可用"
        raise AuthError(
            "browser_login 需要 Playwright，但%s。"
            "处置：pip install playwright；或改用 type=header + token_from 的旧方式。" % reason
        )

    state_path, state_note = ui_auth.storage_state_path(cfg, spec_dir)
    token = None

    if state_path:
        # 会话启动时已把 state 注入浏览器上下文，这里只需把凭据导出来给 HTTP 通道用
        token_from = cfg.get("token_from") or cfg.get("extract")
        if token_from:
            # localStorage 是 per-origin 的，页面还在 about:blank 时读不到目标域的值
            try:
                session.ensure_origin(origin + (cfg.get("login_path") or "/login"))
            except Exception:
                pass
            token = ui_auth.read_storage_value(session, fill(token_from, env_map))
        info["source"] = "storage_state:%s" % os.path.basename(state_path)
        info["note"] = state_note
    else:
        token, _cks, note = ui_auth.run_login_steps(session, cfg, origin, spec_dir, env_map)
        saved = ui_auth.save_state(session, cfg, spec_dir)
        info["source"] = note + ("；已存 %s" % os.path.basename(saved) if saved else "")
        if state_note:
            info["note"] = state_note

    # 浏览器里的 cookie 一律同步给 HTTP 客户端，避免两条通道登录态不一致
    try:
        for c in (session.storage_state() or {}).get("cookies", []):
            cookies[c["name"]] = c["value"]
    except Exception:
        pass

    if token:
        hname = cfg.get("header") or "Authorization"
        prefix = cfg.get("prefix")
        if prefix is None:
            prefix = "Bearer " if hname.lower() == "authorization" else ""
        headers[hname] = "%s%s" % (prefix, token)
        info["header"] = hname
        info["token_masked"] = ui_auth.mask(token)

    if not headers and not cookies:
        raise AuthError(
            "浏览器登录已完成，但没有可用于后续请求的凭据："
            "需配置 token_from（如 localStorage:app-sec-at）或确认登录后有会话 Cookie。"
        )
    info["credential_ok"] = True
    return headers, cookies, info


def authenticate(cfg, base, spec_dir, env_map, client, variables=None, session=None,
                 origin=""):
    """返回 (headers, cookies, info)。

    `base` 是接口基址（含 api_prefix），`origin` 是站点根（不含前缀）。
    session 是 UiSession，只有 browser_login 用得到；其余类型忽略。
    失败抛 AuthError，由 runner 统一转成「环境阻断」。
    """
    origin = (origin or base or "").rstrip("/")
    cfg = cfg or {}
    env_map = env_map or {}
    variables = variables or {}
    typ = cfg.get("type") or ("header" if cfg.get("header") else "none")
    typ = ALIASES.get(typ, typ)
    if typ not in AUTH_TYPES:
        raise AuthError("未知认证类型「%s」，可用：%s" % (typ, "、".join(AUTH_TYPES)))

    info = {"type": typ, "credential_ok": False, "source": "", "note": cfg.get("说明", "")}
    headers, cookies = {}, {}

    if typ == "none":
        info["credential_ok"] = True

    elif typ in ("header", "bearer"):
        value, source = _resolve_secret(cfg, ("token", "value"), spec_dir, env_map)
        if not value:
            raise AuthError("认证类型 %s 未提供凭据（token / token_env / token_from）" % typ)
        hname = cfg.get("header") or ("Authorization" if typ == "bearer" else "Authorization")
        prefix = cfg.get("prefix")
        if prefix is None:
            prefix = "Bearer " if typ == "bearer" else ""
        headers[hname] = "%s%s" % (prefix, value)
        info["source"] = source
        info["header"] = hname
        info["credential_ok"] = True

    elif typ == "cookie":
        value, source = _resolve_secret(cfg, ("cookie", "value"), spec_dir, env_map)
        if not value:
            raise AuthError("认证类型 cookie 未提供凭据（cookie / cookie_env / cookie_from）")
        for seg in value.split(";"):
            if "=" in seg:
                k, v = seg.split("=", 1)
                cookies[k.strip()] = v.strip()
        info["source"] = source
        info["cookie_names"] = list(cookies)
        info["credential_ok"] = True

    elif typ == "form_login":
        login = cfg.get("login") or {}
        if not login.get("path"):
            raise AuthError("form_login 缺少 login.path")
        lv = dict(variables)
        lv.update({"env." + k: v for k, v in env_map.items()})
        local = {}

        # 前置步骤：拿 CSRF / 一次性 nonce
        for pre in login.get("pre_steps") or []:
            resp = client.request(
                pre.get("method", "GET"),
                base + fill(pre["path"], lv),
                headers=fill(pre.get("headers") or {}, lv),
                params=fill(pre.get("params"), lv) if pre.get("params") else None,
            )
            if resp["status"] == 0:
                raise AuthError("登录前置步骤失败：%s" % resp["error"])
            for var, path in (pre.get("提取") or {}).items():
                local[var] = deep_get(resp["json"], path)
        lv.update(local)

        body = fill(login.get("json"), lv) if login.get("json") is not None else None
        form = fill(login.get("form"), lv) if login.get("form") is not None else None
        resp = client.request(
            login.get("method", "POST"),
            base + fill(login["path"], lv),
            headers=fill(login.get("headers") or {}, lv),
            json_body=body,
            form=form,
            timeout=login.get("timeout"),
        )
        if resp["status"] == 0:
            raise AuthError("登录请求未完成：%s" % resp["error"])

        # 判定登录是否成功（规格里没有断言时，用最小默认：2xx 或响应体 success=true）
        default_ok = (200 <= resp["status"] < 400) and not (
            isinstance(resp["json"], dict) and resp["json"].get("success") is False
        )
        expect = login.get("断言") or []
        checks = [judge(a, resp, lv) for a in expect]
        login_ok = default_ok and all(c[0] for c in checks)
        info["login_status"] = resp["status"]
        if not login_ok:
            detail = "；".join(c[1] for c in checks if not c[0]) or "响应不符合登录成功条件"
            raise AuthError(
                "登录失败（HTTP %s）：%s，响应摘要 %s"
                % (resp["status"], detail, (resp["text"] or "")[:200])
            )

        token = None
        for var, path in (login.get("extract") or {}).items():
            val = deep_get(resp["json"], path)
            if val in (None, ""):
                raise AuthError("登录成功但提取不到 %s（路径 %s）" % (var, path))
            if var == "token" or token is None:
                token = val
            local[var] = val

        if token is not None:
            hname = cfg.get("header") or "Authorization"
            prefix = cfg.get("prefix")
            if prefix is None:
                prefix = "Bearer " if hname.lower() == "authorization" else ""
            headers[hname] = "%s%s" % (prefix, token)
            info["header"] = hname
        elif login.get("extract_cookie"):
            cname = login["extract_cookie"]
            if cname not in client.cookies:
                raise AuthError("登录成功但未拿到会话 Cookie「%s」，实际 Cookie：%s"
                                % (cname, list(client.cookies)))
            info["cookie_names"] = list(client.cookies)
        elif not client.cookies:
            raise AuthError(
                "登录成功但没有可用于后续请求的凭据：需在 login.extract 指定 token 路径，"
                "或 login.extract_cookie 指定会话 Cookie 名"
            )
        else:
            info["cookie_names"] = list(client.cookies)

        info["credential_ok"] = True
        info["source"] = "form_login:%s" % login["path"]

    elif typ == "command":
        cmd = cfg.get("command")
        if not cmd:
            raise AuthError("%s 需要 command（脚本 stdout 输出 token 或 {\"token\": ...}）" % typ)
        shell_env = dict(os.environ)
        shell_env.update({k: str(v) for k, v in env_map.items()})
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=cfg.get("timeout", 180), env=shell_env, cwd=spec_dir,
            )
        except subprocess.TimeoutExpired:
            raise AuthError("认证命令超时（%ss）：%s" % (cfg.get("timeout", 180), cmd))
        if proc.returncode != 0:
            raise AuthError(
                "认证命令退出码 %s：%s\nstderr: %s"
                % (proc.returncode, cmd, (proc.stderr or "")[-500:])
            )
        out = (proc.stdout or "").strip()
        if not out:
            raise AuthError("认证命令没有输出任何内容：%s" % cmd)
        token = out
        if out.startswith("{"):
            try:
                obj = json.loads(out)
                token = obj.get("token") or obj.get("access_token") or obj.get("value")
            except ValueError:
                pass
        if not token:
            raise AuthError("认证命令输出里找不到 token：%s" % out[:300])
        hname = cfg.get("header") or "Authorization"
        prefix = cfg.get("prefix")
        if prefix is None:
            prefix = "Bearer " if hname.lower() == "authorization" else ""
        headers[hname] = "%s%s" % (prefix, token)
        info["header"] = hname
        info["source"] = "%s:%s" % (typ, cmd[:60])
        info["credential_ok"] = True

    elif typ == "browser_login":
        headers, cookies, info = _browser_login(
            cfg, base, spec_dir, env_map, variables, session, headers, cookies, info,
            origin=origin,
        )

    if cookies:
        client.cookies.update(cookies)

    # 探活
    probe = cfg.get("probe")
    if probe and info["credential_ok"]:
        resp = client.request(
            probe.get("method", "GET"),
            base + fill(probe["path"], variables),
            headers=headers,
            timeout=probe.get("timeout", 10),
        )
        if resp["status"] == 0:
            raise AuthError("凭据探活请求未完成：%s" % resp["error"])
        pv = dict(variables)
        pv.update({"env." + k: v for k, v in env_map.items()})
        checks = [judge(a, resp, pv) for a in (probe.get("断言") or [])]
        ok = all(c[0] for c in checks) if checks else resp["status"] in (200, 204)
        if not ok:
            detail = "；".join("%s" % c[1] for c in checks if not c[0]) or "HTTP %s" % resp["status"]
            raise AuthError(
                "凭据已失效或不被接受（探活 %s）：%s。请重新生成凭据后重跑。"
                % (probe["path"], detail)
            )
        info["probe"] = {"path": probe["path"], "status": resp["status"], "ok": True}

    return headers, cookies, info
