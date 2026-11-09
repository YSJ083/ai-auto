"""HTTP 客户端 —— 执行器与认证模块共用的唯一出口。

只依赖标准库，避免在客户机器上装依赖。要点：
  - 保留 Cookie 罐，跨步骤自动回传（表单登录/会话型系统必需）
  - HTTP 4xx/5xx 不抛异常，作为正常响应交给断言判定（很多绑定测的正是错误码）
  - 连接层异常归一为 status=0，供上层判定「环境阻断」而不是「产品缺陷」
  - 记录脱敏后的请求头，用于证据留存
"""

import hashlib
import http.cookies
import json
import mimetypes
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SENSITIVE_HEADERS = (
    "authorization",
    "cookie",
    "app-sec-at",
    "x-token",
    "token",
    "x-auth-token",
    "security-data",
)

MAX_BODY_KEEP = 200000

# 单个下载文件的上限。超过就不落盘 —— 落一个被截断的文件，后面的「最小字节」
# 或人工查看都会得出错误结论，不如明确报「超限未保存」。
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# Content-Disposition 里可能带路径穿越（filename="../../etc/passwd"）
_BAD_FILENAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def _as_files(files):
    """把「文件」字段归一成 [(字段名, 路径)]。

    支持三种写法，都是实际会写出来的：
      "/abs/a.pdf"                     → 字段名默认 file
      ["/a.pdf", "/b.pdf"]             → 同上，多文件
      {"file": "/a.pdf", "cover": [...]}  → 显式字段名
    """
    if not files:
        return []
    out = []
    if isinstance(files, dict):
        for name, v in files.items():
            for p in (v if isinstance(v, (list, tuple)) else [v]):
                out.append((str(name), str(p)))
        return out
    for p in (files if isinstance(files, (list, tuple)) else [files]):
        out.append(("file", str(p)))
    return out


def _multipart_body(fields, files):
    """手写 multipart/form-data，返回 (body_bytes, content_type)。

    标准库没有编码器，而「零第三方依赖」是接口通道的硬约束，所以自己拼。
    只实现需要的部分：普通字段（可重复）+ 文件字段，二进制原样透传。
    """
    boundary = "----AiAutoTest%s" % uuid.uuid4().hex
    parts = []
    for name, value in (fields or {}).items():
        for v in (value if isinstance(value, (list, tuple)) else [value]):
            parts.append(("--%s\r\n" % boundary).encode())
            parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
            parts.append(("%s\r\n" % ("" if v is None else v)).encode("utf-8"))
    for name, path in _as_files(files):
        if not os.path.isfile(path):
            raise IOError("上传文件不存在：%s" % os.path.abspath(path))
        fn = os.path.basename(path)
        ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
        parts.append(("--%s\r\n" % boundary).encode())
        parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                      % (name, fn)).encode("utf-8"))
        parts.append(("Content-Type: %s\r\n\r\n" % ctype).encode())
        with open(path, "rb") as f:
            parts.append(f.read())
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


def _header_of(headers, name):
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v or ""
    return ""


def _download_filename(headers, url):
    """从 Content-Disposition / URL 推文件名，并去掉路径部分。"""
    cd = _header_of(headers, "content-disposition")
    name = ""
    m = re.search(r"filename\*=(?:utf-8'')?([^;]+)", cd, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
    if not name:
        m = re.search(r'filename="?([^";]+)"?', cd, re.I)
        if m:
            name = m.group(1).strip()
    if not name:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = _BAD_FILENAME.sub("_", name)
    return name or "download.bin"


def _save_download(raw, headers, dest_dir, url):
    """把响应体落盘，返回产物描述。写入失败也返回描述（带 error），不抛异常 ——
    落盘失败是环境问题，应该由断言报「无法判定」而不是让整轮崩掉。"""
    os.makedirs(dest_dir, exist_ok=True)
    name = _download_filename(headers, url)
    path = os.path.join(dest_dir, name)
    if os.path.exists(path):
        # 同名会互相覆盖：同一用例里下载两个同名文件时，后一个会冲掉前一个，
        # 断言随即读到错的那份。
        stem, ext = os.path.splitext(path)
        i = 2
        while os.path.exists("%s(%d)%s" % (stem, i, ext)):
            i += 1
        path = "%s(%d)%s" % (stem, i, ext)
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except Exception as e:
        return {"name": name, "path": "", "size": len(raw), "sha256": "",
                "url": url, "error": "%s: %s" % (type(e).__name__, e)}
    return {"name": os.path.basename(path), "path": path, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "url": url, "error": ""}



def mask_headers(headers):
    out = {}
    for k, v in (headers or {}).items():
        out[k] = "***" if k.lower() in SENSITIVE_HEADERS else v
    return out


def _merge_cookies(jar, raw):
    """把 Set-Cookie（可能多值，用逗号连接）解析进 jar。"""
    if not raw:
        return
    # SimpleCookie 遇到 Expires 里的逗号会误切分，逐段尝试更稳
    parts = raw.split(", ")
    chunks = []
    buf = ""
    for p in parts:
        buf = (buf + ", " + p) if buf else p
        if "=" in buf:
            chunks.append(buf)
            buf = ""
    for chunk in chunks:
        c = http.cookies.SimpleCookie()
        try:
            c.load(chunk)
        except http.cookies.CookieError:
            continue
        for name, morsel in c.items():
            jar[name] = morsel.value


class HttpClient(object):
    def __init__(self, timeout=15, verify_tls=True, default_headers=None):
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.default_headers = dict(default_headers or {})
        self.cookies = {}
        self._ctx = None
        if not verify_tls:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def fork(self):
        """创建独立会话，供并发 worker 使用；Cookie 不回写主会话。"""
        child = HttpClient(timeout=self.timeout, verify_tls=self.verify_tls,
                           default_headers=self.default_headers)
        child.cookies = dict(self.cookies)
        child.download_dir = getattr(self, "download_dir", "")
        return child

    def cookie_header(self):
        if not self.cookies:
            return ""
        return "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())

    def request(
        self,
        method,
        url,
        headers=None,
        params=None,
        json_body=None,
        form=None,
        raw_body=None,
        timeout=None,
        extra_cookies=None,
        files=None,
        download_dir=None,
    ):
        headers = dict(self.default_headers, **(headers or {}))
        if params:
            qs = urllib.parse.urlencode(params, doseq=True)
            url = url + ("&" if "?" in url else "?") + qs

        data = None
        if files:
            # multipart 优先：带文件的表单通常同时有普通字段，走 form 会丢文件
            data, ctype = _multipart_body(form, files)
            headers.setdefault("Content-Type", ctype)
        elif json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json;charset=UTF-8")
        elif form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif raw_body is not None:
            data = raw_body if isinstance(raw_body, bytes) else str(raw_body).encode("utf-8")

        jar = dict(self.cookies)
        if extra_cookies:
            jar.update(extra_cookies)
        if jar and not any(k.lower() == "cookie" for k in headers):
            headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in jar.items())

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        t0 = time.time()
        status, text, raw_headers, raw = 0, "", {}, b""
        err = None
        limit = MAX_DOWNLOAD_BYTES + 1 if download_dir else MAX_BODY_KEEP
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ctx) as r:
                status = r.status
                raw = r.read(limit)
                raw_headers = dict(r.headers.items())
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read(limit)
            except Exception:
                raw = b""
            raw_headers = dict((e.headers or {}).items())
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
        elapsed = int((time.time() - t0) * 1000)

        downloads = []
        if err:
            text = "请求未完成：%s" % err
        elif download_dir and 200 <= status < 400:
            if len(raw) > MAX_DOWNLOAD_BYTES:
                downloads.append({
                    "name": "", "path": "", "size": len(raw), "sha256": "",
                    "url": url, "truncated": True,
                    "error": "响应体超过单文件上限 %d 字节，未落盘" % MAX_DOWNLOAD_BYTES,
                })
            else:
                downloads.append(_save_download(raw, raw_headers, download_dir, url))
        else:
            # 非 2xx/3xx 响应不会产生文件；正文按错误信息保留，供断言判读
            text = raw[:MAX_BODY_KEEP].decode("utf-8", "replace")

        if status and raw_headers:
            _merge_cookies(self.cookies, raw_headers.get("Set-Cookie"))

        parsed = None
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None

        return {
            "url": url,
            "method": method.upper(),
            "status": status,
            "elapsed_ms": elapsed,
            "json": parsed,
            "text": text,
            "headers": raw_headers,
            "request_headers": mask_headers(headers),
            "error": err,
            "downloads": downloads,
        }
