"""浏览器登录 —— 拿登录态、导出凭据、保存可复用的 storageState。

三级策略（按可靠性排序，能走前一级就不走后一级）：

  L1 storageState 复用
     人登录一次，把整个浏览器态（cookie + localStorage + sessionStorage）存成 json。
     之后每条用例直接加载，无需再登录。有效期通常等于会话有效期，比单个 token 长。

  L2 login_steps 原生填表
     用 UI 步骤跑登录表单，成功后自动存 state，下次走 L1。
     前提：验证码已关闭，或有固定测试账号可绕过。

  L3 验证码
     不做进主流程。需要时用 `auto_test.py login` 打开可见浏览器人工登一次（存 state），
     或用 auth.type=command 挂你自己的打码脚本。
     绕过验证码可能违反被测系统的安全策略，本 skill 不替你做这个决定。

导出的凭据同时喂给两条通道：
  cookie   → HttpClient.cookies，也留在浏览器上下文里
  token    → HTTP 请求头，浏览器侧已天然具备
"""

import json
import os

from .asserts import fill, judge


class AuthStateError(Exception):
    pass


def _abs(path, base_dir):
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))


def storage_state_path(cfg, base_dir):
    """解析并校验 storage_state 配置。返回 (绝对路径, 状态说明)。"""
    p = cfg.get("storage_state") or cfg.get("state_file")
    if not p:
        return "", ""
    ap = _abs(p, base_dir)
    if os.path.isfile(ap):
        size = os.path.getsize(ap)
        if size < 10:
            raise AuthStateError("storageState 文件为空：%s" % ap)
        return ap, "复用登录态：%s（%d 字节）" % (os.path.basename(ap), size)
    if cfg.get("login_steps") or cfg.get("login_url"):
        return "", "登录态文件不存在，将现场登录并生成：%s" % ap
    raise AuthStateError(
        "storageState 文件不存在：%s\n"
        "两种处置：\n"
        "  1. 用 `auto_test.py login --spec <规格> --env <环境>` 打开浏览器人工登录一次，自动存盘；\n"
        "  2. 在 auth 里配 login_steps，让执行器自己填表登录。\n"
        "临时救急也可以把 storage_state 去掉、改用 type=command 输出 token。" % ap
    )


def read_storage_value(session, expr):
    """从浏览器读一个值。expr 支持：
    localStorage:key / sessionStorage:key / cookie:name / js:<表达式> / window.xxx
    """
    if not expr:
        return None
    page = session.page
    try:
        if expr.startswith("localStorage:"):
            return page.evaluate("k => window.localStorage.getItem(k)", expr.split(":", 1)[1])
        if expr.startswith("sessionStorage:"):
            return page.evaluate("k => window.sessionStorage.getItem(k)", expr.split(":", 1)[1])
        if expr.startswith("cookie:"):
            name = expr.split(":", 1)[1]
            for c in session.storage_state().get("cookies", []):
                if c.get("name") == name:
                    return c.get("value")
            return None
        if expr.startswith("js:"):
            return page.evaluate(expr[3:])
        if expr.startswith("window."):
            return page.evaluate("() => %s" % expr)
        # 裸 key：依次试 localStorage / sessionStorage
        v = page.evaluate("k => window.localStorage.getItem(k)", expr)
        if v is None:
            v = page.evaluate("k => window.sessionStorage.getItem(k)", expr)
        return v
    except Exception:
        return None


def run_login_steps(session, cfg, base, spec_dir, env_map):
    """跑登录表单，返回 (token, cookies, 说明)。失败抛 AuthStateError。"""
    steps = cfg.get("login_steps") or []
    login_url = cfg.get("login_url") or cfg.get("login_path") or ""

    variables = dict(env_map)
    variables.update(cfg.get("vars") or {})

    if login_url:
        target = login_url if login_url.startswith("http") else (base + login_url)
        session.goto(target)

    if not steps and not cfg.get("等待人工"):
        raise AuthStateError(
            "browser_login 既没有 storage_state，也没有 login_steps 或 login_url。"
            "无法自动登录，请补配置或改用 login 子命令人工存盘。"
        )

    for i, st in enumerate(steps, 1):
        name = st.get("名称") or st.get("ui") or ("step" + str(i))
        try:
            if st.get("path") or st.get("url"):
                u = st.get("url") or (base + fill(st.get("path"), variables))
                session.goto(u)
            session.act(st, variables)
            session.settle(st)
        except Exception as e:
            raise AuthStateError(
                "登录第 %d 步「%s」失败：%s: %s"
                % (i, name, type(e).__name__, str(e)[:300])
            )

    # 登录成功判定
    success = cfg.get("success")
    if success:
        snap = session.snapshot()
        resp = {
            "url": snap.get("url"), "method": "UI:login", "status": 200,
            "elapsed_ms": 0, "json": snap, "text": snap.get("text") or "",
            "headers": {},
        }
        checks = success if isinstance(success, list) else [success]
        bad = []
        for a in checks:
            try:
                ok, detail = judge(a, resp, variables)
            except Exception as e:
                ok, detail = False, "判定出错：%s" % e
            if not ok:
                bad.append(detail)
        if bad:
            # 登录失败的证据要留：把页面文本带出来，便于判断是账号错还是验证码错
            raise AuthStateError(
                "登录未成功，判定不通过：%s\n页面当前地址：%s\n页面文本片段：%s"
                % ("；".join(bad), resp["url"], (resp["text"] or "")[:300])
            )

    # 导出凭据
    token = None
    token_from = cfg.get("token_from") or cfg.get("extract")
    if token_from:
        token = read_storage_value(session, fill(token_from, variables))
        if token is None:
            raise AuthStateError(
                "登录流程走完，但没从「%s」取到 token。"
                "请确认页面登录后该存储项确实有值，或用 `login` 子命令人工登录后检查。" % token_from
            )

    cookies = {}
    try:
        for c in session.storage_state().get("cookies", []):
            cookies[c.get("name")] = c.get("value")
    except Exception:
        pass

    if not token and not cookies:
        raise AuthStateError("登录流程走完，既没取到 token 也没有 cookie，无法继续。")

    return token, cookies, "表单登录：%d 步" % len(steps)


def save_state(session, cfg, base_dir):
    """登录成功后把整个浏览器态存盘，下次走 L1。"""
    p = cfg.get("save_state")
    if not p:
        return ""
    ap = _abs(p, base_dir)
    try:
        return session.save_storage_state(ap) or ""
    except Exception:
        return ""


def mask(token):
    if not token:
        return ""
    s = str(token)
    return s[:6] + "…" + s[-4:] if len(s) > 12 else "***"
