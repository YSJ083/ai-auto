"""仅 loopback 的内存虚拟依赖服务。

不执行 shell、不修改网络、不接触真实依赖；故障规则仅在本进程内生效。
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class VirtualError(Exception):
    pass


def _matches(match, method, path):
    """只接受精确 method/path 匹配，避免宽泛规则误伤别的请求。"""
    return (isinstance(match, dict)
            and match.get("path") == path
            and str(match.get("method", "")).upper() == method)


class VirtualService:
    def __init__(self, name, routes):
        self.name, self.routes = name, routes or []
        self.faults, self.hits = [], []
        self.server = self.thread = None
        self._lock = threading.Lock()
        self._next_fault_id = 1

    def start(self):
        if self.server is not None:
            return self.base_url
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _go(self):
                path = self.path.split("?", 1)[0]
                method = self.command.upper()
                hit = {"method": method, "path": path, "at_ms": int(time.time() * 1000)}
                with service._lock:
                    rule = next((x for x in service.faults if _matches(x.get("match"), method, path)), None)
                    route = next((r for r in service.routes if _matches(r.get("match"), method, path)), None)
                    hit["fault_id"] = rule.get("id") if rule else None
                    hit["fault_action"] = rule.get("action") if rule else None
                    service.hits.append(hit)

                if rule and rule.get("action") == "delay":
                    time.sleep(rule["毫秒"] / 1000.0)
                if rule and rule.get("action") == "reset":
                    # 关闭连接而非访问任何外部地址，客户端会收到连接重置。
                    self.close_connection = True
                    return
                out = (rule.get("response") if rule and rule.get("action") == "response"
                       else (route or {}).get("response"))
                out = out or {"status": 404, "json": {"error": "route not found"}}
                if "json" in out:
                    raw = json.dumps(out["json"], ensure_ascii=False).encode("utf-8")
                    content_type = "application/json"
                else:
                    raw = str(out.get("text", "")).encode("utf-8")
                    content_type = "text/plain; charset=utf-8"
                self.send_response(out.get("status", 200))
                for key, value in (out.get("headers") or {}).items():
                    self.send_header(key, str(value))
                if "Content-Type" not in (out.get("headers") or {}):
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _go

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.base_url

    @property
    def base_url(self):
        if self.server is None:
            return ""
        return "http://127.0.0.1:%d" % self.server.server_port

    def install_fault(self, rule):
        rule = dict(rule)
        with self._lock:
            rule["id"] = self._next_fault_id
            self._next_fault_id += 1
            self.faults.append(rule)
        return rule["id"]

    def remove_fault(self, fault_id):
        with self._lock:
            self.faults = [x for x in self.faults if x.get("id") != fault_id]

    def clear_faults(self):
        with self._lock:
            self.faults = []

    def evidence(self, start=0):
        with self._lock:
            return list(self.hits[start:])

    def stop(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)
        self.server = self.thread = None
