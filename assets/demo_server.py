#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨引擎汇总示例用的极简本地 API 服务（仅演示，无鉴权、无持久化）。

    python3 assets/demo_server.py --port 8899

故意让 /orders 返回 pageSize=5（忽略请求参数），用来演示失败如何进入汇总报告。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/user/self/info":
            self._send({"success": True, "data": {"id": "u-1001", "mobile": "138****8888"}})
        elif path == "/orders":
            self._send({"success": True, "data": {"pageNo": 2, "pageSize": 5, "total": 12,
                                                 "records": [{"id": "O-11"}, {"id": "O-12"}]}})
        elif path == "/health":
            self._send({"success": True})
        else:
            self._send({"success": False, "message": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"demo server on http://127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
