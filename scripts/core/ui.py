"""浏览器执行通道 —— 把 UI 步骤跑成与 HTTP 步骤同形状的响应。

三条设计约束，任何改动都不能破坏：

1. **响应同形**。UI 步骤必须产出与 httpc 完全一样的 dict
   （url/method/status/elapsed_ms/json/text/headers），UI 特有信息全部塞进 json。
   这样 runner 的变量提取/重试/flaky、report、compare 一行都不用改。

2. **可选依赖**。Playwright 没装时 is_available() 返回 False，调用方把 UI 用例标
   blocked 并写明原因，纯接口用例照常跑。UI 能力缺失绝不能让整轮执行失败。

3. **失败留证据**。截图、控制台报错、失败请求都进响应，写进 result.json，
   报告里能直接看到「当时页面上发生了什么」。

状态语义的区分（重要）：
  动作失败（元素找不到、超时）→ 页面行为不符合预期 → 算 failed（缺陷候选）
  导航失败（连不上、DNS）    → 环境问题             → 算 blocked
  浏览器起不来                → 环境问题             → 整轮 blocked
"""

import hashlib
import os
import re
import tempfile
import time

DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

# 一个步骤内最多记录多少条网络请求明细（可按规格 ui.network_limit 覆盖）。
# 设上限是为了长会话不把内存跑爆；但一旦触顶就置 network_truncated，
# 「网络」断言的次数/请求体判定会拒绝给出结论 —— 截断后的统计是错的，
# 宁可报「无法判定」也不能报一个假结论。
NETWORK_LIMIT = 500

# 请求级「网络」断言前的证据等待预算（毫秒）。
# 动作刚做完就快照，fetch 可能还在路上，会数到 0 次 —— 那是采集时机问题，
# 报出来却像「接口压根没调」，属于纯误报。所以这类步骤先等证据到位再快照。
NETWORK_SETTLE_MS = 3000

# 动作名 → 说明，供 lint 与文档共用
UI_ACTIONS = {
    "点击": "点击元素",
    "填写": "清空并输入文本",
    "选择": "下拉框选择 option",
    "按键": "键盘按键，如 Enter / Control+A",
    "等待": "等元素出现/消失，或纯等待毫秒",
    "悬停": "鼠标悬停",
    "滚动到": "滚动到元素可见",
    "切换frame": "把后续动作的上下文切到 iframe",
    "断言快照": "只取页面快照，不做动作（用于纯断言步骤）",
    "识别验证码": "截图验证码元素并 OCR，结果写入「保存到」指定的变量",
    "上传": "给 <input type=file> 选文件，隐藏的输入框也能选；值为本地绝对路径",
    "下载": "点击触发下载，文件存到「下载目录」，产物描述进响应供「下载」断言判读",
}

def is_available():
    """探测 Playwright 是否可用。返回 (可用, 原因)。

    环境变量 AUTOTEST_NO_UI=1 可强制关掉浏览器通道，用于验证降级行为，
    或在没有图形环境的 CI 机器上明确跳过 UI 用例。
    """
    if os.environ.get("AUTOTEST_NO_UI") in ("1", "true", "yes"):
        return False, "浏览器通道已被 AUTOTEST_NO_UI 显式关闭"
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True, ""
    except ImportError:
        return False, ("未安装 playwright。安装：python3 -m pip install playwright "
                       "（浏览器用系统 Chrome 时无需再执行 playwright install）")
    except Exception as e:  # 装坏了
        return False, "playwright 导入失败：%s: %s" % (type(e).__name__, e)


def _chrome_candidates():
    """系统 Chrome 的常见安装位置。"""
    return [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]


def _cached_chromium():
    """Playwright 缓存目录里的 chromium，按版本倒序取第一个存在的。"""
    roots = [
        os.path.expanduser("~/Library/Caches/ms-playwright"),
        os.path.expanduser("~/.cache/ms-playwright"),
        os.path.expandvars("%LOCALAPPDATA%\\ms-playwright"),
    ]
    found = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root), reverse=True):
            if not name.startswith("chromium-"):
                continue
            base = os.path.join(root, name)
            for sub in ("chrome-mac-arm64", "chrome-mac", "chrome-mac-x64",
                        "chrome-linux", "chrome-win"):
                for exe in ("Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                            "Chromium.app/Contents/MacOS/Chromium",
                            "chrome", "chrome.exe"):
                    p = os.path.join(base, sub, exe)
                    if os.path.isfile(p):
                        found.append(p)
    return found[0] if found else None


def resolve_browser(prefer=None):
    """决定用哪个浏览器。返回 dict(launch_kwargs, note)。

    prefer 可显式指定：
      {"channel": "chrome"}          用系统安装的 Chrome
      {"executable_path": "/path"}   用指定二进制
      {"bundled": true}              用 playwright 自带的（需先 playwright install）
    不指定时按「系统 Chrome → 缓存 chromium → 自带」顺序探测。
    """
    if prefer:
        if prefer.get("executable_path"):
            return ({"executable_path": prefer["executable_path"]},
                    "指定浏览器：%s" % prefer["executable_path"])
        if prefer.get("channel"):
            return ({"channel": prefer["channel"]},
                    "指定 channel：%s" % prefer["channel"])
        if prefer.get("bundled"):
            return ({}, "playwright 自带浏览器")

    for p in _chrome_candidates():
        if os.path.isfile(p):
            return ({"channel": "chrome"}, "系统 Chrome（0 下载）")

    cached = _cached_chromium()
    if cached:
        return ({"executable_path": cached}, "缓存 chromium：%s" % os.path.basename(os.path.dirname(os.path.dirname(cached))))

    return ({}, "playwright 自带浏览器（若启动失败需执行 playwright install chromium）")


_BAD_FILENAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def _safe_filename(name):
    """下载文件名去路径、去非法字符。suggested_filename 来自服务端，不能直接拼。"""
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = _BAD_FILENAME.sub("_", name)
    return name or "download.bin"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


class UiError(Exception):
    """页面动作失败 —— 属于被测系统的行为问题，不是规格错误。"""


class UiSession(object):
    """一个规格复用同一个浏览器上下文。

    生命周期：run_spec 开始前 start，结束后 stop。
    认证与用例共用它，所以登录态是自然延续的，不需要来回搬 cookie。
    """

    def __init__(self, headless=True, timeout=15000, prefer=None,
                 storage_state=None, ignore_https_errors=False, viewport=None,
                 extra_http_headers=None, slow_mo=0, trace=False, network_limit=None,
                 download_dir=None):
        self.headless = headless
        self.timeout = timeout
        self.prefer = prefer or {}
        # 注意：不要叫 self.storage_state —— 那是方法名，被实例属性覆盖后就读不到登录态了
        self._init_state = storage_state
        self.ignore_https_errors = ignore_https_errors
        self.viewport = viewport or dict(DEFAULT_VIEWPORT)
        self.extra_http_headers = extra_http_headers or {}
        self.slow_mo = slow_mo
        self.trace = trace
        self.network_limit = int(network_limit or NETWORK_LIMIT)

        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._frame = None
        self.console_errors = []
        self.page_errors = []
        self.network_failures = []
        self.network_requests = []   # 明细，最多 network_limit 条
        self.network_seen = 0        # 实际收到的响应总数（含未记录的），用于判截断
        self.downloads = []          # 下载产物描述，按步骤窗口切片后进快照
        self.uploads = []            # 上传过的文件路径，进快照当证据
        self.download_dir = download_dir or ""
        self.browser_note = ""
        self.diagnostics = []        # 采集/等待过程的异常留痕，进 result.json
        self.need_text = True        # 用例级：本次是否有断言需要整页文本
        self.selectors = []          # 用例级：本次要快照的选择器
        self.snapshots_dir = None    # 失败截图目录
        self.nav_status = None       # 最近一次导航的主文档 HTTP 状态码

    # ---------------- 生命周期

    def start(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_kwargs, note = resolve_browser(self.prefer)
        self.browser_note = note
        if self.slow_mo:
            launch_kwargs["slow_mo"] = self.slow_mo
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.headless, args=["--disable-dev-shm-usage"], **launch_kwargs
            )
        except Exception as e:
            self.stop()
            raise UiError(
                "浏览器启动失败（%s）：%s: %s。可选处置：装 chromium（playwright install chromium）"
                "或指定 executable_path。" % (note, type(e).__name__, str(e)[:300])
            )

        ctx_kwargs = {
            "viewport": self.viewport,
            "ignore_https_errors": bool(self.ignore_https_errors),
        }
        if self._init_state:
            ctx_kwargs["storage_state"] = self._init_state
        if self.extra_http_headers:
            ctx_kwargs["extra_http_headers"] = self.extra_http_headers
        self._ctx = self._browser.new_context(**ctx_kwargs)
        self._ctx.set_default_timeout(self.timeout)
        self._page = self._ctx.new_page()
        self._hook_events()
        return self

    def _hook_events(self):
        page = self._page

        def guarded(what, fn):
            """回调内异常必须留痕，不能吞掉。

            这四个回调是「控制台」「网络」两类断言的唯一数据来源。回调静默失败，
            证据就是空的，而那两类断言读的正是空列表 —— 结果不是报错，是**判通过**。
            所以这里失败一律进 diagnostics，让报告能显示「这类证据没采全」。
            """
            def run(arg):
                try:
                    fn(arg)
                except Exception as e:
                    self._diag(what, e)
            return run

        def on_console(msg):
            if msg.type == "error":
                self.console_errors.append((msg.text or "")[:500])

        def on_pageerror(err):
            self.page_errors.append(str(err)[:500])

        def on_requestfailed(req):
            self.network_failures.append({
                "url": req.url[:300], "method": req.method,
                "failure": (req.failure or "")[:200],
            })

        def on_response(resp):
            self.network_seen += 1
            if resp.status >= 400:
                self.network_failures.append({
                    "url": resp.url[:300], "method": resp.request.method,
                    "status": resp.status,
                })
            if len(self.network_requests) >= self.network_limit:
                return          # 触顶后只计数不记录，snapshot 会把截断标出来
            req = resp.request
            try:
                body = (req.post_data or "")[:500]
            except Exception:
                body = ""       # 二进制/流式请求体取不到是常态，不算采集失败
            self.network_requests.append({
                "seq": self.network_seen,
                "method": (req.method or "").upper(),
                "url": (resp.url or "")[:300],
                "status": resp.status,
                "type": getattr(req, "resource_type", "") or "",
                "body": body,
            })

        try:
            page.on("console", guarded("浏览器控制台事件回调异常，「控制台」证据可能不完整", on_console))
            page.on("pageerror", guarded("页面 JS 异常捕获回调出错，「控制台」证据可能不完整", on_pageerror))
            page.on("requestfailed", guarded("失败请求捕获回调出错，「网络」证据可能不完整", on_requestfailed))
            page.on("response", guarded("响应拦截回调出错，「网络」证据可能不完整", on_response))
        except Exception as e:
            # 监听器没挂上时 console_errors 恒为空，「控制台无错误」这类断言
            # 就会永远通过 —— 这是最隐蔽的一种假通过，必须留痕。
            self._diag("浏览器事件监听器未能全部挂载，控制台/网络证据可能不完整", e)

    def stop(self):
        for closer in (self._ctx, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._browser = self._page = self._pw = None

    # ---------------- 基本操作

    def _diag(self, what, exc=None):
        """记一条采集过程中的异常：留痕，但不打断执行。

        截图没存成、登录态没写成、cookie 没注进去，这些都不该让用例失败 ——
        但它们也不能被完全吞掉。完全吞掉的结果是报告里「截图空白」「控制台无
        错误」看起来像结论，而实际是采集压根没跑成。所以这里只记录，不抛。
        """
        item = {"what": what}
        if exc is not None:
            item["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        if item in self.diagnostics:      # 同一步反复等超时不必刷屏
            return
        self.diagnostics.append(item)

    @property
    def page(self):
        if self._page is None:
            raise UiError("浏览器会话未启动")
        return self._page

    @property
    def url(self):
        try:
            return self.page.url
        except Exception:
            return ""

    def _target(self):
        """当前动作的作用域：page 或 iframe。"""
        return self._frame or self.page

    def reset_frames(self):
        self._frame = None

    def goto(self, url, wait_until="domcontentloaded", timeout=None):
        """打开页面，并记下主文档的 HTTP 状态码。

        Playwright 对 4xx/5xx 不会抛异常，页面照样"打开成功"。如果只看 URL 变化，
        导航到一个 404 地址也会被当成成功 —— 那种假通过比失败更危险。所以这里把
        导航响应的 status 带出来，交给上层判定。
        """
        self.nav_status = None
        try:
            resp = self.page.goto(url, wait_until=wait_until, timeout=timeout or self.timeout)
            if resp is not None:
                self.nav_status = resp.status
        except Exception as e:
            raise UiError("打开页面失败：%s（%s）" % (url, str(e)[:300]))
        self.reset_frames()
        return self.nav_status

    def ensure_origin(self, url):
        """确保当前页面处在 url 所属的 origin 下。

        localStorage / sessionStorage 是 per-origin 的：页面还停在 about:blank 时去读
        目标站点的 localStorage，拿到的永远是 None。所以读存储之前必须先落到那个域。
        返回是否发生了导航。
        """
        from urllib.parse import urlparse

        cur = urlparse(self.url or "")
        tgt = urlparse(url or "")
        if tgt.netloc and cur.scheme == tgt.scheme and cur.netloc == tgt.netloc:
            return False
        self.goto(url, wait_until="domcontentloaded")
        return True

    def screenshot(self, path):
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self.page.screenshot(path=path, full_page=True)
            return path
        except Exception as e:
            # 报告里截图位空白时，得能看出「是没截成」而不是「页面本来就这样」
            self._diag("失败截图没能保存（%s）" % os.path.basename(path or ""), e)
            return None

    def storage_state(self):
        try:
            return self._ctx.storage_state()
        except Exception as e:
            self._diag("读取浏览器登录态失败", e)
            return None

    def save_storage_state(self, path):
        state = self.storage_state()
        if not state:
            return None
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        import json
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=1)
        except Exception as e:
            self._diag("登录态写盘失败（%s）" % path, e)
            return None
        return path

    def add_cookies(self, cookies):
        """把外部拿到的 cookie 注入浏览器（cookie 形如 {name: value}）。"""
        if not cookies:
            return
        cur = self.url or ""
        domain = ""
        try:
            from urllib.parse import urlparse
            domain = urlparse(cur).hostname or ""
        except Exception:
            pass
        payload = []
        for k, v in cookies.items():
            item = {"name": k, "value": str(v), "path": "/"}
            if domain:
                item["domain"] = domain
            payload.append(item)
        try:
            self._ctx.add_cookies(payload)
        except Exception as e:
            # cookie 没注进去，页面仍是未登录状态，后面会一连串断言不符。
            # 不记这一条，根因就被埋在最下面了。
            self._diag("cookie 注入失败（%d 个，domain=%s）" % (len(payload), domain or "未定"), e)

    # ---------------- 动作

    def _trace_http(self):
        """把页面发出的请求与响应打到 stderr，用于排查「到底哪一步不对」。

        追踪哪些地址由 `AUTOTEST_TRACE_URLS` 决定（逗号分隔的 URL 子串）。
        不设时退化成「所有非 GET 请求 + 所有 4xx/5xx 响应」—— 这条默认规则
        不绑定任何具体系统，换一个被测系统照样有用。早先这里写死了登录接口
        的路径，换系统后整个开关就失效了。

        监听器在每个 page 上只装一次：每次点击都 `page.on` 会叠加，同一条
        请求被打印 N 遍，看起来像「重复提交」，很容易把排查带偏。
        """
        import sys
        if not os.environ.get("AUTOTEST_CAPTCHA_DEBUG"):
            return
        if getattr(self, "_trace_page", None) is self.page:
            return
        self._trace_page = self.page

        pats = [p.strip() for p in os.environ.get("AUTOTEST_TRACE_URLS", "").split(",")
                if p.strip()]

        def _hit(url):
            if pats:
                return any(p in url for p in pats)
            return False

        def _on_request(req):
            try:
                if pats:
                    if not _hit(req.url):
                        return
                elif req.method in ("GET", "OPTIONS", "HEAD"):
                    return
                print("[请求] %s %s %s"
                      % (req.method, req.url.split("?")[0], (req.post_data or "")[:300]),
                      file=sys.stderr)
            except Exception:
                pass

        def _on_response(resp):
            try:
                if pats:
                    if not _hit(resp.url):
                        return
                elif resp.status < 400:
                    return
                body = " ".join(resp.text().split())[:400]
            except Exception as e:
                body = "<读不到:%s>" % e
            try:
                print("[响应] %s %s -> %s"
                      % (resp.status, resp.url.split("?")[0], body), file=sys.stderr)
            except Exception:
                pass

        self.page.on("request", _on_request)
        self.page.on("response", _on_response)

    def act(self, step, variables):
        """执行一个 UI 动作。选择器/值支持 ${变量}。"""
        from .asserts import fill

        act = step.get("ui") or step.get("动作")
        if not act:
            raise UiError("UI 步骤缺少「ui」动作名")
        if act not in UI_ACTIONS:
            raise UiError("未知 UI 动作「%s」，可用：%s" % (act, "、".join(UI_ACTIONS)))

        sel = fill(step.get("选择器"), variables) if step.get("选择器") else None
        val = fill(step.get("值"), variables) if step.get("值") is not None else None
        timeout = step.get("超时") or self.timeout
        idx = step.get("第几个")
        target = self._target()

        if act == "断言快照":
            return

        if act == "切换frame":
            if sel:
                self._frame = self.page.frame_locator(sel)
            else:
                self._frame = None
            return

        if act == "按键":
            key = val or step.get("键") or "Enter"
            if sel:
                target.locator(sel).press(key, timeout=timeout)
            else:
                self.page.keyboard.press(key)
            return

        if act == "等待":
            if sel:
                state = step.get("状态") or "visible"
                target.locator(sel).first.wait_for(state=state, timeout=timeout)
            else:
                self.page.wait_for_timeout(int(step.get("毫秒") or 500))
            return

        if act == "识别验证码":
            # 识别结果写进变量，后续用 ${变量} 填写；识别不出就抛错，
            # 让整条用例失败/重试，绝不静默返回空串制造假通过。
            from . import captcha as captcha_mod

            var = step.get("保存到") or "验证码"
            if sel:
                loc = target.locator(sel)
                if idx is not None:
                    loc = loc.nth(int(idx))
                else:
                    loc = loc.first
                # 验证码常以 data: URL 内嵌，原图通常比渲染尺寸大得多
                # （实测 110x40 渲染成 64x32）。截图会掉分辨率，OCR 直接认错，
                # 所以优先解码 src 拿原图，拿不到再退回截图。
                img = None
                try:
                    src = loc.evaluate("el => el.currentSrc || el.src || ''")
                    if isinstance(src, str) and src.startswith("data:image") and "," in src:
                        try:
                            import base64 as _b64
                        except ImportError:       # 某些环境标准库被改名
                            import base64 as _b64  # type: ignore
                        img = _b64.b64decode(src.split(",", 1)[1])
                except Exception:
                    img = None
                if not img:
                    img = loc.screenshot(timeout=timeout)
            elif step.get("图片地址"):
                from urllib.parse import urljoin

                src = urljoin(self.page.url, fill(step["图片地址"], variables))
                resp = self.page.request.get(src, timeout=timeout)
                img = resp.body()
            else:
                raise UiError("动作「识别验证码」需要「选择器」或「图片地址」")

            try:
                text = captcha_mod.recognize(img, expect_len=step.get("长度"))
            except captcha_mod.CaptchaUnavailable as e:
                raise UiError("验证码无法识别：%s" % e)
            except captcha_mod.CaptchaError as e:
                raise UiError("验证码识别失败：%s" % e)
            variables[var] = text
            return

        if not sel:
            raise UiError("动作「%s」需要「选择器」" % act)

        loc = target.locator(sel)
        if idx is not None:
            loc = loc.nth(int(idx))
        else:
            loc = loc.first

        if act == "上传":
            # 用 set_input_files 而不是模拟点击：站点普遍把 file input 藏起来
            # （display:none + label 包装），点 label 会打开系统文件选择框，
            # 自动化里根本没法操作。直接往 input 塞文件是唯一可靠路径。
            raw = step.get("文件")
            if raw in (None, ""):
                raise UiError("动作「上传」需要「文件」：本地文件路径，多个文件写成数组")
            wanted = raw if isinstance(raw, (list, tuple)) else [raw]
            paths = []
            for one in wanted:
                p = os.path.abspath(str(fill(one, variables)))
                if not os.path.isfile(p):
                    raise UiError("上传文件不存在：%s（相对路径按当前工作目录解析，"
                                  "建议写绝对路径）" % p)
                paths.append(p)
            loc.set_input_files(paths if len(paths) > 1 else paths[0], timeout=timeout)
            self.uploads.append({"selector": sel, "files": paths})
            return

        if act == "下载":
            dest = ""
            if step.get("下载目录"):
                dest = str(fill(step["下载目录"], variables))
            dest = os.path.abspath(dest or self.download_dir
                                   or tempfile.mkdtemp(prefix="autotest-dl-"))
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as e:
                raise UiError("下载目录无法创建：%s（%s）" % (dest, e))
            self._trace_http()
            # 必须用 expect_download 包住点击：下载事件是异步的，点完再找事件
            # 会漏掉，表现成「文件没下下来」——那是采集问题，不是产品缺陷。
            with self.page.expect_download(timeout=timeout) as info:
                loc.click(timeout=timeout)
            dl = info.value
            name = _safe_filename(dl.suggested_filename)
            path = os.path.join(dest, name)
            if os.path.exists(path):
                # 同名互相覆盖：一个用例里下两个同名文件时，后一个会冲掉前一个，
                # 「下载」断言随即读到错的那份，属假证据。
                stem, ext = os.path.splitext(path)
                i = 2
                while os.path.exists("%s(%d)%s" % (stem, i, ext)):
                    i += 1
                path = "%s(%d)%s" % (stem, i, ext)
            item = {"name": os.path.basename(path), "path": path,
                    "suggested": dl.suggested_filename or "",
                    "url": getattr(dl, "url", "") or "", "error": ""}
            try:
                dl.save_as(path)
                item["size"] = os.path.getsize(path)
                item["sha256"] = _sha256(path)
            except Exception as e:
                item["size"] = 0
                item["sha256"] = ""
                item["path"] = ""
                item["error"] = "%s: %s" % (type(e).__name__, e)
            self.downloads.append(item)
            if item["error"]:
                raise UiError("下载已触发但保存失败：%s" % item["error"])
            return

        if act == "点击":
            self._trace_http()
            loc.click(timeout=timeout)
        elif act == "填写":
            loc.fill("" if val is None else str(val), timeout=timeout)
            # 受控组件（React / Arco / Element）常在 blur 时才把 DOM 值同步进组件
            # state，紧接着点提交可能发出空值或旧值。这里主动 blur 一次再等一拍，
            # 保证「填完就点」这种自然写法不会踩到同步延迟。
            try:
                loc.evaluate("el => { if (el.blur) el.blur(); }")
            except Exception:
                pass
            self.page.wait_for_timeout(120)
        elif act == "选择":
            loc.select_option(str(val) if val is not None else "", timeout=timeout)
        elif act == "悬停":
            loc.hover(timeout=timeout)
        elif act == "滚动到":
            loc.scroll_into_view_if_needed(timeout=timeout)
        else:
            raise UiError("动作「%s」尚未实现" % act)

    def wait_network_idle(self, timeout_ms=None):
        """等网络安静下来（有界）。

        有长轮询 / SSE 的页面永远等不满 networkidle，这里超时是常态而不是故障：
        只记一条诊断，不打断执行。
        """
        timeout_ms = int(timeout_ms or NETWORK_SETTLE_MS)
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception as e:
            self._diag("请求级「网络」断言前等待网络空闲超时（%dms），"
                       "本步骤的请求可能没采全" % timeout_ms, e)

    def settle(self, step, after_nav=False):
        """动作后的稳定等待。优先等显式条件，其次等网络空闲，最后兜底短等。

        after_nav=True 时一定会等 networkidle：SPA 用 domcontentloaded 判定「页面就绪」
        是不够的，此时 #app 还是空的，快照会抓到一片空白，断言就会假失败。
        """
        wait = step.get("等待") or {}
        if isinstance(wait, dict) and wait.get("选择器"):
            from .asserts import fill
            sel = wait["选择器"]
            state = wait.get("状态") or "visible"
            self.page.locator(sel).first.wait_for(
                state=state, timeout=wait.get("超时") or self.timeout
            )
            return
        if isinstance(wait, dict) and wait.get("毫秒"):
            self.page.wait_for_timeout(int(wait["毫秒"]))
            return
        if isinstance(wait, str) and wait:
            self.page.locator(wait).first.wait_for(state="visible", timeout=self.timeout)
            return
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        # 步骤可显式关闭 networkidle：个别系统在 networkidle 之后才去取验证码，
        # 等太久反而会错过「图和 captchaId 已经对齐」的时机。
        if step.get("等网络空闲") is False:
            return
        if after_nav or step.get("等网络空闲"):
            idle_ms = int(step.get("网络空闲超时") or 8000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=idle_ms)
            except Exception as e:
                # 有长轮询 / SSE / WebSocket 的页面永远等不到 networkidle，
                # 这里超时是常态而不是故障：记一条，但不打断执行。
                self._diag("networkidle 等待超时（%dms），页面可能存在长连接或仍在加载"
                           % idle_ms, e)

    # ---------------- 快照

    def _probe(self, sel):
        """探测一个选择器在当前页面上的状态。

        `exists=False` 只表示「查过了，页面上确实没有」。探测过程本身出错时
        返回 `probe_failed`，交给断言层拒绝判定 —— 把「查失败」写成「不存在」
        是这套工具最容易产出的假缺陷：页面正在跳转、iframe 已卸载、渲染卡住，
        报告里都会写成「元素没渲染出来」，让人去查一个根本不存在的 bug。
        """
        try:
            loc = self._target().locator(sel)
            n = loc.count()
            info = {"count": n, "exists": n > 0}
            if n == 0:
                return info
            first = loc.first
            try:
                info["visible"] = first.is_visible()
            except Exception:
                info["visible"] = None
            try:
                info["text"] = (first.text_content() or "").strip()[:300]
            except Exception:
                info["text"] = ""
            try:
                info["value"] = first.input_value(timeout=1000)
            except Exception:
                info["value"] = None
            try:
                info["enabled"] = first.is_enabled()
            except Exception:
                info["enabled"] = None
            try:
                info["attrs"] = first.evaluate(
                    "e => {const o={}; for (const a of e.attributes) o[a.name]=a.value; return o;}"
                )
            except Exception:
                info["attrs"] = {}
            return info
        except Exception as e:
            return {"count": None, "exists": None, "probe_failed": True,
                    "error": "%s: %s" % (type(e).__name__, str(e)[:300])}

    def snapshot(self, selectors=None, need_text=None, net_since=None,
                 dl_since=None, up_since=None):
        """抓当前页面快照。

        `need_text=False` 跳过整页 innerText：长表格页面上这一步开销明显，
        而大多数步骤的断言只看元素状态。用例里出现文本类断言时由调用方打开，
        见 `needs_body_text()`。

        `net_since` 传步骤开始时的 `network_seen`，只回传该步骤期间发出的请求。
        「提交只调 1 次」这类断言必须按步骤算 —— 按会话累积算，前面步骤调过
        同一接口就会把 1 变成 2，报出来的是假缺陷。
        """
        sels = selectors if selectors is not None else self.selectors
        if need_text is None:
            need_text = self.need_text
        page = self.page
        since = 0 if net_since is None else int(net_since)
        reqs = [r for r in self.network_requests if r["seq"] > since]
        info = {"url": "", "title": "", "text": "", "elements": {},
                "console_errors": list(self.console_errors),
                "page_errors": list(self.page_errors),
                "network_failures": list(self.network_failures),
                "network_requests": reqs,
                # 记录触顶时统计必错，断言层据此拒绝判定。窗口内实际发出的条数
                # 大于记录条数，就是本步骤被截断了。
                "network_truncated": len(reqs) < (self.network_seen - since),
                # 下载/上传产物同样按步骤窗口切：跨用例累积会让「这一步下载了什么」
                # 读到上一个用例的产物，那是最容易骗过人的假证据。
                "downloads": list(self.downloads[int(dl_since or 0):]),
                "uploads": list(self.uploads[int(up_since or 0):]),
                "diagnostics": list(self.diagnostics)}
        try:
            info["url"] = page.url
        except Exception:
            pass
        try:
            info["title"] = page.title()
        except Exception:
            pass
        if need_text:
            try:
                info["text"] = (page.inner_text("body") or "")[:8000]
            except Exception as e:
                info["text"] = ""
                self._diag("读取页面文本失败，文本类断言将无法判定", e)
        for sel in sels or []:
            if sel:
                info["elements"][sel] = self._probe(sel)
        return info


TEXT_ASSERT_KEYS = ("文本", "文本等于", "不含文本")


def needs_body_text(case):
    """这个用例里有没有断言需要整页文本。

    整页 innerText 在长页面上开销明显，而多数步骤的断言只看元素状态。但
    `包含` / `不包含` 对 UI 步骤比的就是 resp["text"]，「页面」断言里的
    文本类条件也读快照的 text —— 漏判会让这些断言拿到空串，从「通过」
    变成「失败」。所以这里宁可宽一点：只要出现文本类断言就采集。
    """
    def scan(steps):
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            for a in st.get("断言") or []:
                if not isinstance(a, dict):
                    continue
                if a.get("类型") in ("包含", "不包含"):
                    return True
                for k in TEXT_ASSERT_KEYS:
                    if k in a:
                        return True
        return False

    return bool(scan(case.get("steps")) or scan(case.get("setup"))
                or scan(case.get("teardown")))


def collect_selectors(case):
    """收集一个用例里出现的所有选择器，供快照一次性抓取。

    断言用到的选择器也要收 —— 否则断言查不到元素，会误判为「元素不存在」。
    """
    out = []

    def push(v):
        if isinstance(v, str) and v and v not in out:
            out.append(v)

    def scan_steps(steps):
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            push(st.get("选择器"))
            wait = st.get("等待")
            if isinstance(wait, dict):
                push(wait.get("选择器"))
            elif isinstance(wait, str):
                push(wait)
            for a in st.get("断言") or []:
                if isinstance(a, dict):
                    push(a.get("选择器"))

    scan_steps(case.get("steps"))
    scan_steps(case.get("setup"))
    scan_steps(case.get("teardown"))
    for a in case.get("断言") or []:
        if isinstance(a, dict):
            push(a.get("选择器"))
    return out


def request_evidence_patterns(step):
    """这一步「必须看到」的请求：[(方法, URL 片段)]。

    `次数: 0` 是「这个接口不该被调」，等它出现没有意义，跳过。
    """
    out = []
    for a in step.get("断言") or []:
        if not isinstance(a, dict) or a.get("类型") != "网络" or not a.get("请求"):
            continue
        cnt = a.get("次数")
        if cnt == 0 or (isinstance(cnt, dict) and cnt.get("等于") == 0):
            continue
        out.append((str(a.get("方法") or "").upper(), str(a["请求"])))
    return out


def has_request_assertion(step):
    """这一步有没有请求级的「网络」断言（含 `次数: 0` 这种反向断言）。"""
    return any(isinstance(a, dict) and a.get("类型") == "网络" and a.get("请求")
               for a in step.get("断言") or [])


def _request_seen(session, pattern, since=0):
    """本步骤（seq 大于 since 的请求）里有没有出现过该请求。

    必须按 since 过滤：上一个用例也调过同一接口时，扫描全会话会立刻"看见"
    那条旧请求，等待随即返回，本步仍然数到 0 次 —— 白等一场，误报照旧。
    """
    method, fragment = pattern
    for r in getattr(session, "network_requests", None) or []:
        if r.get("seq", 0) <= since:
            continue
        if fragment in (r.get("url") or "") and (not method or r.get("method") == method):
            return True
    return False


def _pump(session, ms):
    """等一小段，同时让浏览器的事件回调跑起来。

    sync Playwright 的事件回调只在当前线程**调用 Playwright API 时**才派发。
    用 time.sleep 等，回调不执行、network_requests 一直是空的 —— 于是
    「调了 2 次」会被稳定地读成「0 次」。实测就是这么翻车的，所以这里必须用
    page.wait_for_timeout（它会回到 driver 循环里跑一轮）。
    """
    try:
        page = session.page
    except Exception:
        page = None
    if page is not None:
        page.wait_for_timeout(ms)
    else:
        time.sleep(ms / 1000.0)


def await_request_evidence(session, step, variables, timeout_ms=None, since=0):
    """等断言要的请求真的发出来，最多 timeout_ms。

    只等「出现过」，不等「次数相符」：次数偏多可能是真缺陷（重复提交），
    等下去只会把缺陷等成超时。
    """
    from .asserts import fill

    timeout_ms = int(timeout_ms if timeout_ms is not None else NETWORK_SETTLE_MS)
    wanted = [(m, str(fill(p, variables)))
              for m, p in request_evidence_patterns(step)]
    if not wanted or not hasattr(session, "network_requests"):
        return True
    deadline = time.time() + timeout_ms / 1000.0
    while True:
        missing = [w for w in wanted if not _request_seen(session, w, since)]
        if not missing:
            return True
        if time.time() >= deadline:
            session._diag(
                "等待请求出现超时（%dms）：%s 本步骤没发出来 —— "
                "请确认动作真的触发了该接口，否则「次数」会被算成 0"
                % (timeout_ms, "、".join("%s %s" % w for w in missing)))
            return False
        _pump(session, 100)


def prepare_request_evidence(session, step, variables, since=0, timeout_ms=None):
    """快照前把请求级「网络」断言需要的证据等到位。

    两步：先等断言里点名的请求出现，再等这一阵请求安静下来（拿到完整次数）。
    任何一步超时都只记诊断、不判失败 —— 采集不全和产品缺陷是两件事，
    混在一起就会产出假缺陷。

    `次数: 0` 这类反向断言也要走这里（只是不等它出现），否则「安静下来」
    这一步会被跳过，晚发的请求还没到就判了「没调过」。
    """
    if not has_request_assertion(step):
        return
    budget = int(timeout_ms if timeout_ms is not None
                else (int(step.get("网络空闲超时") or NETWORK_SETTLE_MS)))
    try:
        await_request_evidence(session, step, variables, timeout_ms=budget, since=since)
        idle = getattr(session, "wait_network_idle", None)
        if idle:
            idle(budget)
    except Exception as e:
        diag = getattr(session, "_diag", None)
        if diag:
            diag("请求级「网络」断言前的等待出错，请求统计可能不准", e)


def run_ui_step(session, step, base, variables):
    """执行一个 UI 步骤，返回与 HTTP 步骤同形状的响应。

    `base` 必须是**站点 origin**（如 http://host:port），不能带 api_prefix ——
    页面路由不属于接口前缀，拼上去会 404。
    """
    from .asserts import fill

    t0 = time.time()
    name = step.get("名称") or step.get("ui") or ""
    act = step.get("ui") or step.get("动作") or ""
    action_failed = False
    nav_failed = False
    error = ""
    nav_status = None
    # 动作前打点：快照只回传本步骤期间发出的请求，供「网络」断言判调用次数
    net_mark = getattr(session, "network_seen", 0)
    dl_mark = len(getattr(session, "downloads", None) or [])
    up_mark = len(getattr(session, "uploads", None) or [])

    try:
        url = step.get("url")
        path = step.get("path")
        navigated = False
        if url or path:
            # url 写相对路径（如 "/login"）是很自然的手误，Playwright 会直接失败
            # 并把页面留在 about:blank，后续断言全成"假失败"。这里统一按 origin 拼。
            target = url or path
            if not str(target).startswith(("http://", "https://")):
                target = base.rstrip("/") + fill(str(target), variables)
            nav_status = session.goto(
                target, wait_until=step.get("等页面") or "domcontentloaded",
                timeout=step.get("超时"),
            )
            session.settle(step, after_nav=True)
            navigated = True
        session.act(step, variables)
        if not navigated:
            session.settle(step)
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, str(e)[:400])
        if isinstance(e, UiError) and "打开页面失败" in str(e):
            nav_failed = True
        else:
            action_failed = True

    # 请求级「网络」断言要基于「这一步真的发出的请求」。动作刚结束就快照，
    # fetch 可能还在路上 —— 数到 0 次会被读成「接口没调」，是纯误报。
    prepare_request_evidence(session, step, variables, since=net_mark)

    snap = {}
    try:
        snap = session.snapshot(net_since=net_mark, dl_since=dl_mark, up_since=up_mark)
    except Exception as e:
        snap = {"elements": {}, "text": "", "url": "", "error": str(e)[:200]}

    text = snap.get("text") or ""
    if error:
        text = (text + "\n[执行错误] " + error).strip()

    # 导航到 4xx/5xx 时，页面渲染出来的任何东西都不是被测对象，后续断言没有意义。
    # 用例若自己写了「状态码」断言，交给它判，这里不重复；没写就补一条前置检查，
    # 避免"URL 变了就算通过"这种假通过。
    precheck = []
    declared_status = any(a.get("类型") == "状态码" for a in (step.get("断言") or []))
    if nav_status is not None and nav_status >= 400 and not declared_status:
        precheck.append({
            "assertion": {"类型": "状态码", "等于": "<2xx/3xx>", "来源": "导航自动检查"},
            "passed": False,
            "actual": "导航到 %s 返回 HTTP %s —— 页面本身没打开，后续断言不成立"
                      % (snap.get("url") or session.url, nav_status),
        })

    return {
        "url": snap.get("url") or session.url,
        "method": "UI:%s" % (act or "快照"),
        # status 只表达「这一步有没有导航、导航回了什么码」：
        #   导航了        → 主文档 HTTP 状态码
        #   没导航没出错  → None（根本没有状态码可言）
        #   出错且没导航  → 0（通道级失败）
        # 早先没导航时给 200，于是挂在点击步骤上的
        # {"类型":"状态码","等于":200} 恒为真 —— 一条永远通过的断言比没有更糟。
        "status": nav_status if nav_status is not None else (0 if error else None),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "json": snap,
        "text": text,
        "headers": {},
        "request_headers": {},
        "error": error,
        "ui": True,
        "ui_action_failed": action_failed,
        "ui_nav_failed": nav_failed,
        "nav_status": nav_status,
        "precheck": precheck,
        "diagnostics": snap.get("diagnostics") or [],
        # 「下载」断言的证据：本步骤落到磁盘的产物（名称/路径/字节数/摘要）
        "downloads": snap.get("downloads") or [],
        # 报告要能区分「页面真的是空的」和「这一步压根没去读文本」
        "text_collected": bool(getattr(session, "need_text", True)),
        "name": name,
    }
