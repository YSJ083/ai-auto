#!/usr/bin/env python3
"""AI 自动测试执行器 —— 规格 → 执行 → 判定 → 报告。

子命令：
  lint     检查规格能不能跑（不执行）
  run      执行规格，产出 result.json + 测试报告
  compile  把带「执行」块的用例编译成规格
  compare  两次结果对比，识别新增失败 / 已修复 / 仍失败

退出码（供 CI 门禁使用）：
  0  全部通过
  1  有失败（缺陷候选）
  2  规格错误 / 参数错误
  3  环境阻断（连不上、凭据失效）—— 与 1 区分，避免把环境问题当产品问题
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import compile as compile_mod  # noqa: E402
from core import report as report_mod  # noqa: E402
from core import runner  # noqa: E402
from core.env import resolve as resolve_env  # noqa: E402


def cmd_lint(args):
    from core.runner import load_spec
    spec = load_spec(args.spec)
    problems = compile_mod.lint_spec(spec, base_dir=os.path.dirname(os.path.abspath(args.spec)))
    err = [p for p in problems if p[0] == "error"]
    warn = [p for p in problems if p[0] == "warning"]
    print("规格：%s  v%s  用例 %d 条" % (spec.get("name"), spec.get("spec_version"), len(spec["cases"])))
    for lv, msg in err:
        print("  [错误] %s" % msg)
    for lv, msg in warn:
        print("  [提示] %s" % msg)
    if not err and not warn:
        print("  没有问题，可以执行。")
    print("\n结论：%d 个错误，%d 个提示" % (len(err), len(warn)))
    if err or (args.strict and warn):
        return 2
    return 0


def cmd_run(args):
    result = runner.run_spec(
        args.spec,
        out_dir=args.out,
        only=args.only,
        env_name=args.env,
        env_file=args.env_file,
        retry_override=args.retry,
        quiet=args.quiet,
        fail_fast=args.fail_fast,
        junit_path=args.junit,
    )
    s = result["summary"]
    if args.json:
        print(json.dumps({"summary": s, "target": result["target"], "paths": result.get("_paths")},
                         ensure_ascii=False, indent=1))
    if not result["target"]["credential_ok"]:
        print("\n环境阻断：凭据未就绪。修好凭据再重跑，本轮结果不作为质量结论。", file=sys.stderr)
        return 3
    if s["failed"]:
        return 1
    if s["blocked"]:
        return 3
    return 0


def cmd_compile(args):
    with open(args.cases, encoding="utf-8") as f:
        payload = json.load(f)
    spec, problems = compile_mod.compile_and_save(
        payload,
        args.out,
        name=args.name,
        base_url=args.base_url,
        api_prefix=args.api_prefix,
        env=args.env,
        spec_version=args.spec_version,
        base_dir=os.path.dirname(os.path.abspath(args.cases)),
    )
    err = [p for p in problems if p[0] == "error"]
    warn = [p for p in problems if p[0] == "warning"]
    for lv, msg in err:
        print("  [错误] %s" % msg)
    for lv, msg in warn:
        print("  [提示] %s" % msg)
    print("\n已生成：%s（%d 条用例，跳过 %d 条有错误的用例）"
          % (args.out, len(spec["cases"]), len(err)))
    if err or (args.strict and warn):
        print("存在错误：规格已生成，但有错误的用例不会被执行。先修用例再跑。")
        return 2
    return 0


def cmd_compare(args):
    base = json.load(open(args.baseline, encoding="utf-8"))
    cur = json.load(open(args.current, encoding="utf-8"))

    def index(r):
        return {c["id"]: c for c in r["cases"]}

    b, c = index(base), index(cur)
    new_fail, fixed, still_fail, new_case, gone = [], [], [], [], []
    for cid, case in c.items():
        if cid not in b:
            new_case.append(case)
            continue
        bs, cs = b[cid]["status"], case["status"]
        if bs == "passed" and cs == "failed":
            new_fail.append(case)
        elif bs == "failed" and cs == "passed":
            fixed.append(case)
        elif bs == "failed" and cs == "failed":
            still_fail.append(case)
    for cid, case in b.items():
        if cid not in c:
            gone.append(case)

    L = ["# 回归对比", ""]
    L.append("- 基线：%s（%s）" % (base["run"].get("started_at"), len(base["cases"])))
    L.append("- 本次：%s（%s）" % (cur["run"].get("started_at"), len(cur["cases"])))
    L.append("")
    L.append("| 类别 | 数量 |")
    L.append("|---|---|")
    L.append("| 新增失败 | %d |" % len(new_fail))
    L.append("| 已修复 | %d |" % len(fixed))
    L.append("| 仍失败 | %d |" % len(still_fail))
    L.append("| 新增用例 | %d |" % len(new_case))
    L.append("| 本次未执行 | %d |" % len(gone))
    L.append("")
    if new_fail:
        L.append("## 新增失败（优先看这些）")
        L.append("")
        for x in new_fail:
            L.append("- **%s** %s：%s" % (x["id"], x["title"], (x.get("reason") or "")[:160]))
        L.append("")
    if fixed:
        L.append("## 已修复")
        L.append("")
        for x in fixed:
            L.append("- %s %s" % (x["id"], x["title"]))
        L.append("")
    if still_fail:
        L.append("## 仍失败")
        L.append("")
        for x in still_fail:
            L.append("- %s %s" % (x["id"], x["title"]))
        L.append("")

    md = "\n".join(L)
    out = args.out or os.path.join(os.path.dirname(args.current), "回归对比.md")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print("已写入：%s" % out)
    return 1 if new_fail else 0


def cmd_login(args):
    """打开可见浏览器，人工登录一次，把整个浏览器态存下来。

    这是解决图形验证码的现实办法：验证码自动识别不靠谱，但人可以点一次。
    存下的 storageState 之后被每条用例复用，人工介入从「每次跑测试」降为「定期刷一次」。
    """
    from core import ui as ui_mod
    from core.runner import load_spec

    spec_path = os.path.abspath(args.spec)
    spec = load_spec(spec_path)
    spec_dir = os.path.dirname(spec_path)
    ctx = resolve_env(spec, spec_dir, args.env, args.env_file)
    auth = ctx.get("auth") or {}

    ok, why = ui_mod.is_available()
    if not ok:
        print(why, file=sys.stderr)
        return 2

    if args.check:
        launch, note = ui_mod.resolve_browser((ctx.get("ui") or {}).get("browser"))
        print("Playwright：已安装")
        print("浏览器：%s" % note)
        print("参数：%s" % launch)
        return 0

    login_url = auth.get("login_url") or auth.get("login_path") or "/login"
    # 登录页属于站点路由，不吃 api_prefix；拼上前缀会 404
    url = login_url if str(login_url).startswith("http") else ctx["base_url"].rstrip("/") + login_url

    target = args.out or auth.get("storage_state") or auth.get("save_state") or "seeds/state.json"
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(spec_dir, target))

    session = ui_mod.UiSession(
        headless=False,
        prefer=(ctx.get("ui") or {}).get("browser") or {},
        ignore_https_errors=not ctx["verify_tls"],
    )
    print("打开浏览器：%s" % url)
    session.start()
    try:
        session.goto(url)
    except Exception as e:
        print("打开页面失败：%s" % e, file=sys.stderr)
        session.stop()
        return 3

    print()
    print("请在浏览器窗口里手动完成登录（含验证码）。")
    print("确认已进入登录后的页面，再回到这里。")
    try:
        input("按回车保存登录态（Ctrl+C 放弃）...")
    except (KeyboardInterrupt, EOFError):
        session.stop()
        print("已放弃，未保存。")
        return 2

    saved = session.save_storage_state(target)
    snapshot = session.snapshot()
    try:
        ls_keys = session.page.evaluate("() => Object.keys(window.localStorage)")
    except Exception:
        ls_keys = []
    session.stop()

    if not saved:
        print("保存失败：没能拿到浏览器状态", file=sys.stderr)
        return 2
    print("\n已保存：%s" % saved)
    print("当前页面：%s" % (snapshot.get("url") or ""))
    print("localStorage 键：%s" % (", ".join(ls_keys) if ls_keys else "（空）"))
    if ls_keys and len(ls_keys) < 2:
        print("警告：localStorage 只有 %d 个键。若被测前端除 token 外还依赖用户信息或路由表，"
              "只注入 token 会让页面空白 —— 一定要用本命令产出的完整 state，别手工拼。" % len(ls_keys))
    print("\n下一步：把规格里的 auth.storage_state 指向这个文件，即可自动复用。")
    print("提示：登录态过期时执行会被探活拦为环境阻断，重新跑一次本命令即可。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="auto_test", description="AI 自动测试执行器")
    ap.add_argument("--version", action="version", version="ai-auto-test %s" % runner.VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="执行规格")
    p.add_argument("spec")
    p.add_argument("--out", help="结果目录，默认 <规格目录>/测试结果")
    p.add_argument("--env", help="环境档名，对应 environments.json 里的键")
    p.add_argument("--env-file", help="环境档文件，默认 <规格目录>/environments.json")
    p.add_argument("--only", help="只跑 id 以此前缀开头的用例，如 TC-AUTH")
    p.add_argument("--retry", type=int, help="覆盖规格里的重试次数")
    p.add_argument("--fail-fast", action="store_true", help="第一条失败即停止")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help="末尾输出机器可读摘要")
    p.add_argument("--junit", help="JUnit XML 输出路径，默认写到 <out>/junit.xml")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("lint", help="检查规格")
    p.add_argument("spec")
    p.add_argument("--strict", action="store_true", help="有提示也算失败")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("compile", help="用例 → 规格")
    p.add_argument("cases")
    p.add_argument("--out", required=True)
    p.add_argument("--name")
    p.add_argument("--base-url", default="")
    p.add_argument("--api-prefix", default="")
    p.add_argument("--env", default="default")
    p.add_argument("--spec-version", default="1.0")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("compare", help="两次结果对比")
    p.add_argument("baseline")
    p.add_argument("current")
    p.add_argument("--out")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("login", help="打开浏览器人工登录一次，保存 storageState")
    p.add_argument("spec")
    p.add_argument("--env")
    p.add_argument("--env-file")
    p.add_argument("--out", help="state 保存路径，默认取 auth.storage_state 或 seeds/state.json")
    p.add_argument("--check", action="store_true", help="只检查 Playwright 与浏览器是否就绪")
    p.set_defaults(func=cmd_login)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print("文件不存在：%s" % e, file=sys.stderr)
        return 2
    except ValueError as e:
        print("规格或参数有问题：%s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
