"""httpc.py 的单元测试 —— 重点在 multipart 编码与下载落盘。

为什么值得单独测：这两条路径的错法都很安静。
  - multipart 少一个 \\r\\n，服务端解析不出文件，返回的却是 200；
  - 下载文件名带路径穿越，文件被写到目录外面，断言读到的是上一次的旧文件。
两种都不会报错，只会让报告说「通过」。

用进程内的 HTTPServer 兜住真实收发，不联网、不依赖被测系统。
"""

import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from core import httpc as H          # noqa: E402


# ---------------------------------------------------------------- 纯函数

class TestFileList(unittest.TestCase):

    def test_字符串默认字段名file(self):
        self.assertEqual(H._as_files("/tmp/a.pdf"), [("file", "/tmp/a.pdf")])

    def test_数组多文件(self):
        self.assertEqual(H._as_files(["/a.pdf", "/b.pdf"]),
                         [("file", "/a.pdf"), ("file", "/b.pdf")])

    def test_字典给显式字段名(self):
        got = H._as_files({"avatar": "/a.png", "docs": ["/1.pdf", "/2.pdf"]})
        self.assertEqual(got, [("avatar", "/a.png"), ("docs", "/1.pdf"),
                               ("docs", "/2.pdf")])

    def test_空值返回空列表(self):
        self.assertEqual(H._as_files(None), [])
        self.assertEqual(H._as_files(""), [])


class TestMultipart(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _file(self, name, data=b"hello"):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_boundary与content_type一致(self):
        p = self._file("a.txt")
        body, ctype = H._multipart_body({}, {"file": p})
        boundary = ctype.split("boundary=")[1]
        self.assertIn(b"--" + boundary.encode(), body)
        self.assertTrue(body.endswith(("--%s--\r\n" % boundary).encode()))

    def test_文件段落带filename与content_type(self):
        p = self._file("报告.pdf", b"%PDF-1.4")
        body, _ = H._multipart_body({}, {"file": p})
        self.assertIn(b'name="file"; filename="', body)
        self.assertIn(b"application/pdf", body)
        self.assertIn(b"%PDF-1.4", body)

    def test_普通字段与文件同发(self):
        p = self._file("a.txt")
        body, _ = H._multipart_body({"biz": "order", "tag": ["x", "y"]},
                                    {"file": p})
        self.assertIn(b'name="biz"', body)
        self.assertIn(b"order", body)
        self.assertEqual(body.count(b'name="tag"'), 2)

    def test_二进制原样透传(self):
        raw = bytes(range(256))
        p = self._file("b.bin", raw)
        body, _ = H._multipart_body({}, {"file": p})
        self.assertIn(raw, body)

    def test_文件不存在当场报错(self):
        with self.assertRaises(IOError):
            H._multipart_body({}, {"file": os.path.join(self.dir, "nope.pdf")})


class TestDownloadName(unittest.TestCase):

    def test_优先filename(self):
        h = {"Content-Disposition": 'attachment; filename="导出.xlsx"'}
        self.assertEqual(H._download_filename(h, "http://h/export"), "导出.xlsx")

    def test_支持filename_star编码(self):
        h = {"Content-Disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E8%A1%A8.csv"}
        self.assertEqual(H._download_filename(h, "http://h/export"), "报表.csv")

    def test_没有头就用URL末段(self):
        self.assertEqual(H._download_filename({}, "http://h/a/b/data.csv"), "data.csv")

    def test_URL末段要解码(self):
        self.assertEqual(
            H._download_filename({}, "http://h/%E6%8A%A5%E8%A1%A8.csv"), "报表.csv")

    def test_路径穿越被剥掉(self):
        h = {"Content-Disposition": 'attachment; filename="../../etc/passwd"'}
        self.assertEqual(H._download_filename(h, "http://h/x"), "passwd")

    def test_非法字符被替换(self):
        h = {"Content-Disposition": 'attachment; filename="a:b*c?.txt"'}
        self.assertEqual(H._download_filename(h, "http://h/x"), "a_b_c_.txt")

    def test_什么都没有给兜底名(self):
        self.assertEqual(H._download_filename({}, "http://h/"), "download.bin")


class TestSaveDownload(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_落盘并记字节数与摘要(self):
        d = H._save_download(b"abcdef", {"Content-Disposition": 'attachment; filename="x.bin"'},
                             self.dir, "http://h/x")
        self.assertEqual(d["name"], "x.bin")
        self.assertEqual(d["size"], 6)
        with open(d["path"], "rb") as f:
            self.assertEqual(f.read(), b"abcdef")
        self.assertEqual(len(d["sha256"]), 64)

    def test_同名文件加序号不互相覆盖(self):
        hd = {"Content-Disposition": 'attachment; filename="r.csv"'}
        first = H._save_download(b"1", hd, self.dir, "http://h/x")
        second = H._save_download(b"2", hd, self.dir, "http://h/x")
        self.assertEqual(first["name"], "r.csv")
        self.assertEqual(second["name"], "r(2).csv")
        with open(first["path"], "rb") as f:
            self.assertEqual(f.read(), b"1")

    def test_目录不存在自动建(self):
        sub = os.path.join(self.dir, "a", "b")
        d = H._save_download(b"x", {"Content-Disposition": 'filename="y.txt"'},
                             sub, "http://h/y")
        self.assertTrue(os.path.isfile(d["path"]))


# ---------------------------------------------------------------- 真实收发

class _Handler(BaseHTTPRequestHandler):
    """一个记账的小服务：/upload 回显收到的字段，/file 回一个附件。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/file"):
            body = b"column\n1\n2\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            # 中文文件名必须走 filename* 编码：HTTP 头是 latin-1，直接写中文
            # 会让服务端在编码响应头时抛异常，连接直接被掐断。
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%E6%95%B0%E6%8D%AE.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/boom"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write("服务端炸了".encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type") or ""
        has_file = b'filename="' in raw
        # 把「收到什么」编成 JSON 回给测试，避免在这里依赖 json 解析
        out = ("{\"ctype_ok\": %s, \"has_file\": %s, \"bytes\": %d}"
               % ("true" if "multipart/form-data" in ctype else "false",
                  "true" if has_file else "false", len(raw))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class TestRealRequests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.client = H.HttpClient(timeout=5)

    def url(self, p):
        return "http://127.0.0.1:%d%s" % (self.port, p)

    def test_上传真的发出multipart(self):
        p = os.path.join(self.dir, "a.txt")
        with open(p, "wb") as f:
            f.write(b"payload")
        r = self.client.request("POST", self.url("/upload"), files={"file": p})
        self.assertEqual(r["status"], 200)
        self.assertTrue(r["json"]["ctype_ok"])
        self.assertTrue(r["json"]["has_file"])

    def test_上传同时带表单字段(self):
        p = os.path.join(self.dir, "a.txt")
        with open(p, "wb") as f:
            f.write(b"x")
        r = self.client.request("POST", self.url("/upload"), form={"biz": "1"},
                                files={"file": p})
        self.assertEqual(r["status"], 200)
        self.assertTrue(r["json"]["has_file"])

    def test_上传文件不存在不会静默发出空请求(self):
        with self.assertRaises(IOError):
            self.client.request("POST", self.url("/upload"),
                                files={"file": os.path.join(self.dir, "nope")})

    def test_下载落盘并给出文件名(self):
        r = self.client.request("GET", self.url("/file"), download_dir=self.dir)
        self.assertEqual(r["status"], 200)
        d = r["downloads"][0]
        self.assertEqual(d["name"], "数据.csv")
        self.assertEqual(d["size"], len(b"column\n1\n2\n"))
        self.assertTrue(os.path.isfile(d["path"]))
        # 二进制不被塞进 text，避免把正文当 JSON 解析
        self.assertEqual(r["text"], "")

    def test_下载接口返500不产生文件但保留错误正文(self):
        r = self.client.request("GET", self.url("/boom"), download_dir=self.dir)
        self.assertEqual(r["status"], 500)
        self.assertEqual(r["downloads"], [])
        self.assertIn("服务端炸了", r["text"])

    def test_不配下载目录时downloads为空(self):
        r = self.client.request("GET", self.url("/file"))
        self.assertEqual(r["downloads"], [])
        self.assertIn("column", r["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
