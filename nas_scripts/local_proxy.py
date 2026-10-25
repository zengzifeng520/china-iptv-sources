# -*- coding: utf-8 -*-
"""
本地组播转 HTTP 代理
====================
把运营商内网的 rtp://239.x.x.x:port 变成播放器能直接打开的 http 地址。

    rtp://239.77.0.4:5146  ->  http://127.0.0.1:8888/udp/239.77.0.4:5146

前提：运行本程序的电脑必须连着对应运营商的宽带（组播只在城域网内可达）。

用法：
    python local_proxy.py                      # 默认 0.0.0.0:8888
    python local_proxy.py -p 9000
    python local_proxy.py --line 广东-电信      # 同时生成整条线路的播放列表

播放入口：
    http://127.0.0.1:8888/playlist.m3u         # 线路播放列表
    http://127.0.0.1:8888/udp/239.77.0.4:5146  # 单个频道
    http://127.0.0.1:8888/status               # 查看本机组播网卡情况
"""
import argparse
import os
import re
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MDIR = os.path.join(HERE, "output", "multicast")

STREAM_LINE = ""      # 当前线路名，用于生成 playlist
PROXY_BASE = ""       # http://127.0.0.1:8888


def open_multicast(ip: str, port: int, iface: str = "0.0.0.0", timeout: int = 8):
    """加入组播组，返回 socket"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    except OSError:
        pass
    # Windows 必须绑到 0.0.0.0:port
    s.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(ip), socket.inet_aton(iface))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(timeout)
    return s


def guess_iface():
    """猜本机出口网卡的 IP，用于绑定组播网卡"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_text(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            return self._send_text(200, (
                f"组播转 HTTP 代理运行中\n\n"
                f"播放入口：\n"
                f"  线路列表  {PROXY_BASE}/playlist.m3u\n"
                f"  单个频道  {PROXY_BASE}/udp/239.77.0.4:5146\n"
                f"  状态      {PROXY_BASE}/status\n"))

        if path == "/status":
            ip = guess_iface()
            return self._send_text(200, (
                f"本机网卡出口 IP：{ip}\n"
                f"监听：{PROXY_BASE}\n"
                f"当前线路：{STREAM_LINE or '(未指定)'}\n\n"
                f"注意：只有在对应运营商宽带下，组播才能收到数据。\n"))

        if path.startswith("/playlist"):
            return self._playlist()

        m = re.match(r"^/(?:udp|rtp)/(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$", path)
        if m:
            return self._stream(m.group(1), int(m.group(2)))

        self._send_text(404, "not found\n")

    def _playlist(self):
        if not STREAM_LINE:
            lines = sorted({os.path.splitext(f)[0] for f in os.listdir(MDIR) if f.endswith(".txt")})
            return self._send_text(200, "未指定线路，用 --line 启动。可用线路：\n" + "\n".join(lines))
        src = os.path.join(MDIR, STREAM_LINE + ".txt")
        if not os.path.exists(src):
            return self._send_text(404, f"没有 {STREAM_LINE}")

        out = ["#EXTM3U", f"# {STREAM_LINE} 经本地代理",]
        n = 0
        for raw in open(src, encoding="utf-8"):
            s = raw.strip()
            if not s or s.startswith("#") or "," not in s:
                continue
            name, url = s.split(",", 1)
            mm = re.search(r"rtp://(\d+\.\d+\.\d+\.\d+):(\d+)", url)
            if not mm:
                continue
            out.append(f'#EXTINF:-1 tvg-name="{name}",{name}')
            out.append(f"{PROXY_BASE}/udp/{mm.group(1)}:{mm.group(2)}")
            n += 1
        body = "\n".join(out)
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        print(f"[playlist] {STREAM_LINE} -> {n} 个频道")

    def _stream(self, ip, port):
        print(f"[open] {ip}:{port}")
        try:
            sock = open_multicast(ip, port, IFACE)
        except OSError as e:
            print(f"[fail] {ip}:{port} 加入组播失败: {e}")
            return self._send_text(502, f"加入组播失败: {e}\n")

        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        total, idle = 0, 0
        t0 = time.time()
        try:
            while True:
                try:
                    pkt, _ = sock.recvfrom(65535)
                except socket.timeout:
                    idle += 1
                    if total == 0 or idle >= 2:
                        print(f"[timeout] {ip}:{port} 无数据（网络不在该运营商内？）")
                        break
                    continue
                idle = 0
                self.wfile.write(pkt)
                total += len(pkt)
                if total > 4 * 1024 * 1024 * 1024:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
            dur = time.time() - t0
            print(f"[close] {ip}:{port}  {total/1024:.0f} KB / {dur:.1f}s")


IFACE = "0.0.0.0"


def main():
    global STREAM_LINE, PROXY_BASE, IFACE
    ap = argparse.ArgumentParser()
    ap.add_argument("-H", "--host", default="0.0.0.0")
    ap.add_argument("-p", "--port", type=int, default=8888)
    ap.add_argument("--line", default="", help="线路名，如 广东-电信")
    ap.add_argument("--iface", default="", help="组播网卡 IP，默认 0.0.0.0 自动")
    a = ap.parse_args()

    STREAM_LINE = a.line
    IFACE = a.iface or "0.0.0.0"
    host_for_url = "127.0.0.1" if a.host in ("0.0.0.0", "") else a.host
    PROXY_BASE = f"http://{host_for_url}:{a.port}"

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print("=" * 56)
    print(f"  组播转 HTTP 代理   监听 {a.host}:{a.port}")
    print(f"  本机出口 IP        {guess_iface()}")
    print(f"  线路               {STREAM_LINE or '(未指定，加 --line)'}")
    print(f"  播放列表           {PROXY_BASE}/playlist.m3u")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
