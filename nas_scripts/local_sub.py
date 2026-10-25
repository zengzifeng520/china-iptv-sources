# -*- coding: utf-8 -*-
"""
本地订阅服务（常驻）
====================
给你一个永远不变的订阅地址，播放器只认这个：

    http://127.0.0.1:8888/sub.m3u

它自己处理三件事：
  1. 频道骨架存在本地（组播地址，如 239.77.0.185:5146），不依赖任何 token
  2. 中转池里自动挑一台活着的服务器（121.10.7.16:4000 这类）
  3. 每 10 分钟体检一次，挂了自动切换到池里其它中转

用法：
    python local_sub.py                       # 启动，默认 8888 端口
    python local_sub.py -p 9000
    python local_sub.py --add 1.2.3.4:4000    # 加一台中转进池
    python local_sub.py --list                # 看中转池状态

接口：
    /sub.m3u     订阅地址（给播放器）
    /status      中转池状态
    /add?host=IP:端口
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "output", "site")

# 支持容器部署：数据目录、端口、种子中转都可用环境变量覆盖
DATA = os.getenv("DATA_DIR", HERE)
POOL = os.path.join(DATA, "relays.json")
CORE = os.path.join(DATA, "channels_core.json")
SEED = os.getenv("SEED_RELAYS", "121.10.7.16:4000")
# 设置后强制把全部频道归到这一个分组；留空则保留频道原有分组
SUB_GROUP = os.getenv("SUB_GROUP", "").strip()
LINES = int(os.getenv("LINES_PER_CHANNEL", "3"))   # 每个频道给几条线路（取最快的）
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36"

LOCK = threading.Lock()
CHANNELS = []      # [{group,name,mcast}]
RELAYS = []        # [{host, mode, ok, kbps, last_check}]
ACTIVE = {"host": "", "mode": "rtp"}


# ---------- 骨架 ----------
def build_core(src=None):
    """从播放列表里抽出频道骨架：名字 + 组播地址（去掉中转部分）"""
    src = src or os.path.join(SITE, "精选.m3u")
    if not os.path.exists(src):
        return []
    raw = open(src, encoding="utf-8-sig", errors="ignore").read().replace("\r", "")
    out, grp, name = [], "", ""
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            m = re.search(r",(.+?)\s*$", s)
            name = m.group(1).strip() if m else ""
            g = re.search(r'group-title="([^"]*)"', s)
            grp = g.group(1) if g else ""
            continue
        if s.startswith("#"):
            continue
        m = re.search(r"/(rtp|udp)/+(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", s)
        if not m:
            m = re.search(r"(rtp|udp)://(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", s)
        if m:
            out.append(dict(group=grp, name=name, mcast=f"{m.group(2)}:{m.group(3)}",
                            mode=m.group(1)))
    return out


def load_pool():
    global RELAYS
    if os.path.exists(POOL):
        try:
            RELAYS = json.load(open(POOL, encoding="utf-8")).get("relays", [])
        except Exception:
            RELAYS = []
    if not RELAYS and CHANNELS:
        seed = os.path.join(SITE, "精选.m3u")
        if os.path.exists(seed):
            txt = open(seed, encoding="utf-8", errors="ignore").read(4000)
            m = re.search(r"https?://([^/\s]+)/(rtp|udp)/", txt)
            if m:
                RELAYS = [dict(host=m.group(1), mode=m.group(2), ok=None,
                               kbps=0, last_check="")]
    if not RELAYS and SEED:
        RELAYS = [dict(host=h.strip(), mode="rtp", ok=None, kbps=0, last_check="")
                  for h in SEED.split(",") if h.strip()]
    # 恢复上次选中的中转
    try:
        data = json.load(open(POOL, encoding="utf-8")) if os.path.exists(POOL) else {}
    except Exception:  # noqa: BLE001
        data = {}
    ah = (data or {}).get("active_host") or ""
    if ah and any(r["host"] == ah for r in RELAYS):
        ACTIVE["host"] = ah
        ACTIVE["mode"] = next(r.get("mode", "rtp") for r in RELAYS if r["host"] == ah)


def save_pool():
    """把中转池和「当前选中的那台」一起落盘，重启后能恢复，不会退回池序第一。"""
    json.dump(dict(relays=RELAYS, active_host=ACTIVE.get("host", "")),
              open(POOL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ---------- 体检 ----------
_POOL_MTIME = [0.0]
# 低于这个码率就考虑换更快的；新线路要比现在快 _MIN_GAIN 倍才切，避免抖动
SLOW_KBPS = int(os.getenv("SLOW_KBPS", "4500"))
MIN_GAIN = float(os.getenv("MIN_GAIN", "1.35"))
RESELECT_COOLDOWN = int(os.getenv("RESELECT_COOLDOWN", "1800"))
_last_reselect = [0.0]
_reselect_lock = threading.Lock()


def _maybe_reload():
    """中转池文件被外部改动（harvest_relays.py / 手工编辑）时自动重载"""
    try:
        m = os.path.getmtime(POOL)
    except OSError:
        return
    if m != _POOL_MTIME[0]:
        _POOL_MTIME[0] = m
        load_pool()


# 中转换路后主动刷新用户订阅：否则要等整点 cron + extra_source 1 小时缓存，最多滞后 2 小时
SYNC_SCRIPT = os.getenv("SYNC_SCRIPT", "/vol1/1000/docker/china-iptv-sources/sync_users.py")
SYNC_MIN_GAP = int(os.getenv("SYNC_MIN_GAP", "900"))
_sync_last = [0.0]


def notify_relay_changed(old, new):
    if not new or old == new:
        return
    if not os.path.exists(SYNC_SCRIPT):
        return
    now = time.time()
    if now - _sync_last[0] < SYNC_MIN_GAP:
        print(f"[同步] 距上次刷新不足 {SYNC_MIN_GAP}s，跳过")
        return
    _sync_last[0] = now
    d = os.path.dirname(SYNC_SCRIPT)
    try:
        env = dict(os.environ, EXTRA_FORCE="1")
        with open(os.path.join(d, "users_sync.log"), "a") as lf:
            subprocess.Popen([sys.executable, "-u", SYNC_SCRIPT], cwd=d, env=env,
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        print(f"[同步] 中转 {old or '(空)'} -> {new}，已触发用户订阅刷新")
    except Exception as exc:  # noqa: BLE001
        print(f"[同步] 触发失败：{exc}")


def probe_relay(host, mode="rtp", secs=3, n=2):
    """随便挑 n 个频道打一下，能出 20KB 以上就算活"""
    if not CHANNELS:
        return False, 0
    step = max(1, len(CHANNELS) // max(n, 1))
    picks = CHANNELS[::step][:n]
    total, good = 0, 0
    for ch in picks:
        url = f"http://{host}/{mode}/{ch['mcast']}"
        # 单次探测会偶发假失败（中转瞬时抖动），失败再试一次才判死
        size = 0
        for attempt in (0, 1):
            try:
                r = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", str(secs),
                                    "-w", "%{size_download}", "-A", UA, url],
                                   capture_output=True, text=True, timeout=secs + 6)
                size = int(r.stdout.strip() or 0)
            except Exception:
                size = 0
            if size > 20000:
                break
        total += size
        if size > 20000:
            good += 1
    kbps = int(total * 8 / (secs * len(picks)) / 1000) if picks else 0
    return good > 0, kbps


def refresh_relays():
    _maybe_reload()
    with LOCK:
        prev = ACTIVE["host"]
        pool = list(RELAYS)
    for r in pool:
        ok, kbps = probe_relay(r["host"], r.get("mode", "rtp"), n=1)
        with LOCK:
            r["ok"] = ok
            r["kbps"] = kbps
            r["last_check"] = datetime.now().strftime("%H:%M:%S")
            if ok:
                r["last_ok"] = datetime.now().strftime("%m-%d %H:%M")
    alive = [r for r in pool if r.get("ok")]
    with LOCK:
        if alive:
            cur = next((r for r in alive if r["host"] == ACTIVE["host"]), None)
            # 粘性：当前这台还活着就继续用，避免两台速度相近时每轮来回切
            if cur is not None:
                ACTIVE["mode"] = cur.get("mode", "rtp")
            else:
                # 需要重新选：按中转池顺序取第一台活的（池序即优先级，主力排前面），
                # 不按瞬时码率，避免测速抖动导致频繁换台
                first = next(r for r in pool if r.get("ok"))
                ACTIVE["host"] = first["host"]
                ACTIVE["mode"] = first.get("mode", "rtp")
        else:
            ACTIVE["host"] = ""
        save_pool()
    notify_relay_changed(prev, ACTIVE["host"])
    return len(alive)


def reselect(reason=""):
    """串行测速（并发测会互相抢带宽，数字不准），挑最快的一台设为当前。"""
    if not _reselect_lock.acquire(blocking=False):
        return
    try:
        with LOCK:
            pool = [r for r in RELAYS if r.get("ok")]
            cur = ACTIVE["host"]
        if not pool:
            return
        best = None
        for r in pool:
            ok, kbps = probe_relay(r["host"], r.get("mode", "rtp"), secs=5, n=1)
            with LOCK:
                r["ok"] = ok
                r["kbps"] = kbps if ok else 0
                if ok:
                    r["last_ok"] = datetime.now().strftime("%m-%d %H:%M")
                r["last_check"] = datetime.now().strftime("%H:%M:%S")
            if ok and (best is None or kbps > best[1]):
                best = (r, kbps)
        _last_reselect[0] = time.time()
        if not best:
            return
        with LOCK:
            cur_kbps = next((r.get("kbps", 0) for r in RELAYS if r["host"] == cur), 0)
            # 当前这台还够快，或者新的没快多少，就不折腾
            if cur != best[0]["host"] and cur_kbps and best[1] < cur_kbps * MIN_GAIN:
                print(f"[重选] {reason} 最快是 {best[0]['host']}({best[1]})，"
                      f"但当前 {cur}({cur_kbps}) 差距不大，保持")
                save_pool()
                return
            ACTIVE["host"] = best[0]["host"]
            ACTIVE["mode"] = best[0].get("mode", "rtp")
            save_pool()
        notify_relay_changed(cur, best[0]["host"])
        if cur != best[0]["host"]:
            print(f"[重选] {reason} {cur or '(空)'} -> {best[0]['host']} ({best[1]} kbps)")
        else:
            print(f"[重选] {reason} 保持 {cur} ({best[1]} kbps)")
    finally:
        _reselect_lock.release()


def maybe_reselect(reason=""):
    """当前线路明显偏慢、且离上次重选够久，才在后台跑一次串行测速"""
    if time.time() - _last_reselect[0] < RESELECT_COOLDOWN:
        return False
    with LOCK:
        host = ACTIVE["host"]
        kbps = next((r.get("kbps", 0) for r in RELAYS if r["host"] == host), 0)
        fastest = max([r.get("kbps", 0) for r in RELAYS if r.get("ok")] or [0])
    if not host or kbps >= SLOW_KBPS or fastest < kbps * MIN_GAIN:
        return False
    threading.Thread(target=reselect, args=(reason,), daemon=True).start()
    return True


def check_active():
    """只探当前在用的那一台（1 个频道，3 秒），失败立刻全量重选"""
    with LOCK:
        host, mode = ACTIVE["host"], ACTIVE["mode"]
    if not host:
        return refresh_relays()

    ok, kbps = probe_relay(host, mode, secs=3, n=1)
    with LOCK:
        for r in RELAYS:
            if r["host"] == host:
                r["ok"] = ok
                if ok:
                    r["kbps"] = kbps or r.get("kbps", 0)
                    r["last_ok"] = datetime.now().strftime("%m-%d %H:%M")
                r["last_check"] = datetime.now().strftime("%H:%M:%S")
    if ok:
        maybe_reselect("自检发现当前偏慢")
        return sum(1 for r in RELAYS if r.get("ok"))

    print(f"[切换] {host} 失联，重新选路")
    return refresh_relays()


def watchdog(fast, full):
    # 启动先全量体检一次
    n = refresh_relays()
    print(f"[体检] {len(RELAYS)} 台中转，可用 {n} 台，当前 {ACTIVE['host']}")
    last_full = time.time()
    while True:
        time.sleep(fast)
        try:
            if time.time() - last_full >= full:
                n = refresh_relays()
                last_full = time.time()
                print(f"[全面体检] {datetime.now():%H:%M:%S} 可用 {n}/{len(RELAYS)}，当前 {ACTIVE['host']}")
            else:
                check_active()
        except Exception as exc:  # noqa: BLE001
            print(f"[体检异常] {exc}")


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _text(self, code, s, ctype="text/plain; charset=utf-8"):
        d = s.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        path, _, qs = self.path.partition("?")

        if path in ("/sub.m3u", "/sub", "/"):
            return self._sub()

        if path == "/status":
            with LOCK:
                lines = [f"频道骨架  {len(CHANNELS)} 个",
                         f"当前中转  {ACTIVE['host'] or '(无可用)'}  风格 {ACTIVE['mode']}",
                         f"检查时间  {datetime.now():%Y-%m-%d %H:%M:%S}", "",
                         f"{'状态':<6}{'中转':<24}{'码率':>10}  上次检查"]
                for r in RELAYS:
                    st = "√" if r.get("ok") else ("?" if r.get("ok") is None else "×")
                    lines.append(f"{st:<6}{r['host']:<24}{r.get('kbps',0):>7} kbps  {r.get('last_check','')}")
            return self._text(200, "\n".join(lines))

        if path in ("/status.html", "/panel"):
            return self._status_html()

        if path == "/reselect":
            threading.Thread(target=reselect, args=("手动触发",), daemon=True).start()
            return self._text(200, "已开始串行测速（约 1 分钟），稍后刷新 /status.html 查看")

        if path == "/add":
            m = re.search(r"host=([^&\s]+)", qs)
            if not m:
                return self._text(400, "用法： /add?host=1.2.3.4:4000 [&mode=rtp|udp]")
            host = m.group(1)
            mm = re.search(r"mode=(rtp|udp)", qs)
            mode = mm.group(1) if mm else ACTIVE.get("mode", "rtp")
            with LOCK:
                if any(r["host"] == host for r in RELAYS):
                    return self._text(200, f"{host} 已在池里")
                RELAYS.append(dict(host=host, mode=mode, ok=None, kbps=0, last_check=""))
                save_pool()
            threading.Thread(target=refresh_relays, daemon=True).start()
            return self._text(200, f"已加入 {host}（{mode}），正在体检")

        self._text(404, "not found")

    def _status_html(self):
        with LOCK:
            rows = list(RELAYS)
            host, mode = ACTIVE["host"], ACTIVE["mode"]
            nch = len(CHANNELS)
        alive = sum(1 for r in rows if r.get("ok"))
        ip = lan_ip()

        body = []
        for r in rows:
            ok = r.get("ok")
            cls = "ok" if ok else ("dead" if ok is False else "wait")
            mark = "正常" if ok else ("失效" if ok is False else "待检")
            act = ' class="act"' if r["host"] == host else ""
            body.append(
                f'<tr{act}><td><span class="dot {cls}"></span>{mark}</td>'
                f'<td class="h">{r["host"]}</td><td>{r.get("mode","rtp")}</td>'
                f'<td class="num">{r.get("kbps",0) or "—"}</td>'
                f'<td class="num">{r.get("last_check","") or "—"}</td>'
                f'<td class="num">{r.get("last_ok","") or "—"}</td></tr>')

        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>中转池状态</title><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0f1115;color:#e6e8ec;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:900px;margin:0 auto;padding:28px 20px 50px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:#8b93a1;font-size:12px;margin-bottom:22px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}}
.card{{background:#171a21;border:1px solid #232833;border-radius:10px;padding:14px 16px}}
.card .n{{font-size:24px;font-weight:700;color:#4ea1ff}}
.card .l{{color:#8b93a1;font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:#8b93a1;font-weight:500;font-size:12px;padding:8px 10px;border-bottom:1px solid #232833}}
td{{padding:9px 10px;border-bottom:1px solid #1b1f27}}
tr.act td{{background:#131a26}}
.h{{font-family:ui-monospace,Consolas,monospace;color:#9cc4f0}}
.num{{font-variant-numeric:tabular-nums;color:#9aa4b2}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}}
.dot.ok{{background:#31c48d;box-shadow:0 0 6px #31c48d}}
.dot.dead{{background:#e05252}}
.dot.wait{{background:#c9a227}}
.code{{background:#171a21;border:1px solid #232833;border-radius:8px;padding:12px 14px;
font-family:ui-monospace,Consolas,monospace;font-size:12.5px;color:#a9b6c9;margin-top:20px;word-break:break-all}}
</style></head><body><div class="wrap">
<h1>中转池状态</h1>
<div class="sub">每 30 秒自动刷新　·　{datetime.now():%Y-%m-%d %H:%M:%S}</div>
<div class="cards">
<div class="card"><div class="n">{alive}<span style="color:#5f6775;font-size:15px"> / {len(rows)}</span></div><div class="l">可用中转</div></div>
<div class="card"><div class="n" style="font-size:15px;padding-top:6px">{host or "无可用"}</div><div class="l">当前在用（{mode}）</div></div>
<div class="card"><div class="n">{nch}</div><div class="l">频道数</div></div>
</div>
<table><thead><tr><th>状态</th><th>中转</th><th>风格</th><th>码率 kbps</th><th>上次检查</th><th>最后存活</th></tr></thead>
<tbody>{''.join(body)}</tbody></table>
<div class="code">播放器订阅：http://{ip}:{self.server.server_address[1]}/sub.m3u</div>
</div></body></html>"""
        self._text(200, html, "text/html; charset=utf-8")

    def _sub(self):
        with LOCK:
            host, mode = ACTIVE["host"], ACTIVE["mode"]
            chs = list(CHANNELS)
        if not host:
            return self._text(503, "#EXTM3U\n# 中转池里没有可用服务器，看 /status 或 --add 加一台\n",
                              "audio/x-mpegurl; charset=utf-8")
        with LOCK:
            alive = [dict(r) for r in RELAYS if r.get("ok")]
        alive.sort(key=lambda r: (r["host"] != host, -r.get("kbps", 0)))
        lines = alive[:LINES] or [dict(host=host, mode=mode)]   # 当前用的排第一
        out = ["#EXTM3U", f"# 本地订阅 {datetime.now():%Y-%m-%d %H:%M} 中转 {host}"]
        for c in chs:
            grp = SUB_GROUP or c["group"]
            for r in lines:
                out.append(f'#EXTINF:-1 group-title="{grp}",{c["name"]}')
                out.append(f"http://{r['host']}/{r.get('mode', mode)}/{c['mcast']}")
        self._text(200, "\n".join(out), "audio/x-mpegurl; charset=utf-8")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global CHANNELS
    os.makedirs(DATA, exist_ok=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", type=int, default=int(os.getenv("PORT", "8888")))
    ap.add_argument("-H", "--host", default=os.getenv("BIND", "0.0.0.0"))
    ap.add_argument("--add", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fast", type=int, default=int(os.getenv("FAST_CHECK", "180")),
                    help="当前中转的快速自检间隔（秒）")
    ap.add_argument("--full", type=int, default=int(os.getenv("FULL_SWEEP", "1800")),
                    help="全池体检间隔（秒）")
    ap.add_argument("--interval", type=int, default=0, help="已废弃，等同 --full")
    ap.add_argument("--rebuild", action="store_true", help="重新从 精选.m3u 抽取骨架")
    a = ap.parse_args()

    # 骨架
    if os.path.exists(CORE):
        CHANNELS = json.load(open(CORE, encoding="utf-8"))
    if not CHANNELS or a.rebuild:
        CHANNELS = build_core()
        if CHANNELS:
            json.dump(CHANNELS, open(CORE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"频道骨架  {len(CHANNELS)} 个")
    if not CHANNELS:
        print("\n没有频道骨架。请把 channels_core.json 放到数据目录，")
        print("或把 精选.m3u 放到 output/site/ 后加 --rebuild 重建。")
        return
    load_pool()

    if a.add:
        with LOCK:
            if not any(r["host"] == a.add for r in RELAYS):
                RELAYS.append(dict(host=a.add, mode="rtp", ok=None, kbps=0, last_check=""))
                save_pool()
        print(f"已加入中转 {a.add}")

    if a.list:
        for r in RELAYS:
            st = "√" if r.get("ok") else ("?" if r.get("ok") is None else "×")
            print(f"  {st}  {r['host']:<24}{r.get('kbps',0):>8} kbps")
        return

    if not RELAYS:
        print("\n中转池是空的。先加一台：")
        print("  python local_sub.py --add 121.10.7.16:4000")
        return

    full = a.full or a.interval or 1800
    threading.Thread(target=watchdog, args=(a.fast, full), daemon=True).start()
    time.sleep(1.2)

    srv = ThreadingHTTPServer((a.host, a.port), H)
    srv.daemon_threads = True
    ip = lan_ip()
    print("=" * 58)
    print(f"  订阅地址（播放器填这个）  http://{ip}:{a.port}/sub.m3u")
    print(f"  本机访问                  http://127.0.0.1:{a.port}/sub.m3u")
    print(f"  中转池状态                http://{ip}:{a.port}/status")
    print(f"  分组                      {SUB_GROUP or '(保留原始分组)'}")
    print(f"  快速自检                  {a.fast} 秒（只在用的那台）")
    print(f"  全池体检                  {a.full or a.interval or 1800} 秒")
    print("=" * 58)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
