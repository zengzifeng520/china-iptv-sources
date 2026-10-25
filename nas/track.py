#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""track.py —— 用户订阅访问计数（心跳），给 admin.py 出“几个人在用”

原理：不改用户的订阅地址，只在每份 m3u 里把节目单地址 x-tvg-url 和“订阅信息”那一行的
台标换成本服务的地址。播放器加载订阅 / 刷新节目单时会请求这里，本服务记一笔日志，
然后 302 跳到真正的 EPG（或返回 1x1 透明图），播放体验不变。

  GET /epg/<token>.xml   记一笔 -> 302 到真实 EPG
  GET /logo/<token>.png  记一笔 -> 1x1 PNG
  GET /beat?u=<token>    记一笔 -> 1x1 PNG
  GET /health            健康检查

日志：hits.jsonl  {"t": 时间戳, "ip": 客户端IP, "ua": User-Agent, "tok": 用户 token, "p": 路径}
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE, "users.json")
HITS_FILE = os.path.join(BASE, "hits.jsonl")
EPG_URL = os.environ.get("EPG_URL", "https://zengzifeng.ccwu.cc/output/epg.xml")
PORT = int(os.environ.get("TRACK_PORT", "8898"))

# 1x1 透明 PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)

_lock = threading.Lock()
_tokens = {"ts": 0.0, "set": set()}


def known_tokens():
    """users.json 里的 token 集合，10 秒缓存。"""
    now = time.time()
    if now - _tokens["ts"] > 10:
        try:
            with open(USERS_FILE, encoding="utf-8") as f:
                users = json.load(f)
            _tokens["set"] = {str(v.get("token", "")) for v in users.values()}
        except Exception:  # noqa: BLE001
            pass
        _tokens["ts"] = now
    return _tokens["set"]


def log_hit(tok, ip, ua, path):
    rec = {"t": int(time.time()), "ip": ip, "ua": (ua or "")[:160], "tok": tok, "p": path}
    with _lock:
        try:
            with open(HITS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "IPTVTrack/1.0"

    def log_message(self, fmt, *args):  # 静默，避免刷屏
        pass

    def client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        qs = parse_qs(p.query)

        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        tok = ""
        if path.startswith("/epg/"):
            tok = path[len("/epg/"):].split(".xml")[0]
        elif path.startswith("/logo/"):
            tok = path[len("/logo/"):].split(".png")[0]
        elif path == "/beat":
            tok = (qs.get("u") or [""])[0]

        if tok and tok in known_tokens():
            log_hit(tok, self.client_ip(), self.headers.get("User-Agent", ""), path)

        if path.startswith("/epg/"):
            self.send_response(302)
            self.send_header("Location", EPG_URL)
            self.send_header("Cache-Control", "max-age=300")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # 台标 / beat：返回 1x1 PNG
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "max-age=600")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)


if __name__ == "__main__":
    print("track listening on 0.0.0.0:%d, log=%s" % (PORT, HITS_FILE), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
