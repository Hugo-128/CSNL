#!/usr/bin/env python3
"""
COMP41720 Lab 3 - 可开关的 LLM 代理（混沌测试辅助）

把流量从 :11435 转发到真实 Ollama (:11434)。现场演示时：
  - 正常运行 = 代理在跑，服务能正常调用 LLM
  - 模拟宕机 = 停掉本代理（kill），服务连接被拒绝 -> 熔断打开
  - 恢复     = 重启本代理，服务 HALF-OPEN 试探成功 -> 熔断关闭

服务进程 (app.py) 全程不重启，LLM_API_URL 固定指向 http://localhost:11435。

用法:
  python3 llm_proxy.py                 # 前台运行（Ctrl+C 停止）
  python3 llm_proxy.py &               # 后台运行，kill 停止
"""

import socketserver
import threading
import urllib.parse
import urllib.request

LISTEN_PORT = 11435
UPSTREAM = "http://localhost:11434"


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self._read_request()
            if not data:
                return
            # 解析请求行与 Content-Length
            head, _, body = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            if not lines:
                return
            method, path, _ = lines[0].split(b" ", 2)
            headers = {}
            for line in lines[1:]:
                if b":" in line:
                    k, v = line.split(b":", 1)
                    headers[k.decode().lower()] = v.decode().strip()
            content_length = int(headers.get("content-length", "0"))
            # 读满 body（简单起见，已读到则跳过）
            body = body[:content_length]

            url = UPSTREAM + path.decode()
            req = urllib.request.Request(
                url,
                data=body if body else None,
                method=method.decode(),
            )
            req.add_header("Content-Type", headers.get("content-type", "application/json"))

            # 绕过系统代理，直连本地 Ollama
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=120) as resp:
                resp_body = resp.read()
                self.request.sendall(
                    b"HTTP/1.1 " + str(resp.status).encode() + b" " + resp.reason.encode() + b"\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(resp_body)).encode() + b"\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    + resp_body
                )
        except Exception as e:  # noqa: BLE001 - proxy must never crash
            try:
                err = str(e).encode()
                self.request.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(err)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + err
                )
            except Exception:  # noqa: BLE001
                pass

    def _read_request(self):
        self.request.settimeout(5)
        chunks = []
        try:
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\r\n\r\n" in b"".join(chunks):
                    # 粗略：读到 header 结束即返回（body 已含在内）
                    return b"".join(chunks)
        except Exception:  # noqa: BLE001
            pass
        return b"".join(chunks) if chunks else None


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadingServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    print(f"LLM proxy listening on 127.0.0.1:{LISTEN_PORT} -> {UPSTREAM}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nproxy stopped")
