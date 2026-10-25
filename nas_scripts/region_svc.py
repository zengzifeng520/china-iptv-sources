# -*- coding: utf-8 -*-
"""region_svc.py —— 多地区中转池：每个地区一套频道 + 自己的中转，独立体检与切换。

数据文件 data/regions.json：
{"regions":[{"key":"上海","name":"上海",
             "channels":[{"name":"东方卫视","mcast":"239.49.1.100:8000","group":"上海","mode":"udp"}],
             "relays":[{"host":"1.2.3.4:4022","mode":"udp","ok":true,"kbps":8000,"last_check":"19:30"}],
             "active":{"host":"1.2.3.4:4022","mode":"udp"}}]}
"""
import json
import os
import subprocess
import threading
from datetime import datetime

UA = "Mozilla/5.0 (compatible; region-svc/1.0)"
MIN_BYTES = 20000
LINES = int(os.getenv("LINES_PER_CHANNEL", "3"))   # 每个频道给几条线路（取最快的）


class Region:
    def __init__(self, d):
        self.key = str(d.get("key") or "")
        self.name = str(d.get("name") or self.key)
        self.group = str(d.get("group") or (f"{self.name}节目" if self.name != "广东" else "电信组播"))
        ch = d.get("channels")
        self.channels = ch if isinstance(ch, list) else []
        self.relays = d.get("relays") or []
        self.active = d.get("active") or {"host": "", "mode": "udp"}
        self.lock = threading.Lock()

    # ---------- 探测 ----------
    def _fetch(self, host, mode, mcast, secs):
        url = f"http://{host}/{mode}/{mcast}"
        for _ in range(2):          # 失败重试一次，避免瞬时抖动误杀
            try:
                r = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", str(secs),
                                    "-w", "%{size_download}", "-A", UA, url],
                                   capture_output=True, text=True, timeout=secs + 6)
                n = int(r.stdout.strip() or 0)
            except Exception:  # noqa: BLE001
                n = 0
            if n > MIN_BYTES:
                return n
        return 0

    def _samples(self, n=2):
        out = []
        step = max(1, len(self.channels) // max(n, 1))
        for c in self.channels[::step][:n]:
            out.append(c.get("mcast"))
        return [m for m in out if m]

    def _probe(self, host, mode, secs=4):
        """地区频道是各节点的并集，一台中转只认其中一部分。
        所以依次试多个候选频道，能拉到任意一个就算这台活着。"""
        picks = self._samples(8)
        if not picks:
            return 0
        for mc in picks:
            n = self._fetch(host, mode, mc, secs)
            if n:
                return int(n * 8 / secs / 1000)
        return 0

    # ---------- 体检 / 选路 ----------
    def refresh(self):
        with self.lock:
            relays = list(self.relays)
        for r in relays:
            k = self._probe(r.get("host", ""), r.get("mode", "udp"))
            with self.lock:
                r["ok"] = bool(k)
                r["kbps"] = k
                r["last_check"] = datetime.now().strftime("%H:%M:%S")
                if k:
                    r["last_ok"] = datetime.now().strftime("%m-%d %H:%M")
        alive = [r for r in relays if r.get("ok")]
        prev = self.active.get("host", "")
        with self.lock:
            if alive:
                cur = next((r for r in alive if r["host"] == prev), None)
                if cur is None:                      # 挂了才换，按池序取第一台活的
                    cur = alive[0]
                    self.active = {"host": cur["host"], "mode": cur.get("mode", "udp")}
                else:
                    self.active["mode"] = cur.get("mode", "udp")
            else:
                self.active = {"host": "", "mode": "udp"}
            new = self.active.get("host", "")
        return len(alive), prev, new

    def check_active(self):
        host = self.active.get("host") or ""
        mode = self.active.get("mode", "udp")
        if not host:
            self.refresh()
            return
        picks = self._samples(1)
        ok = bool(picks and self._fetch(host, mode, picks[0], 3))
        with self.lock:
            for r in self.relays:
                if r.get("host") == host:
                    r["ok"] = ok
                    r["last_check"] = datetime.now().strftime("%H:%M:%S")
                    if ok:
                        r["last_ok"] = datetime.now().strftime("%m-%d %H:%M")
        if not ok:
            self.refresh()

    # ---------- 输出 ----------
    def entries(self):
        """返回 [(EXTINF, url)]，供主订阅按地区分组拼接"""
        host = self.active.get("host") or ""
        mode = self.active.get("mode", "udp")
        if not host:
            return []
        with self.lock:
            alive = [dict(r) for r in self.relays if r.get("ok")]
        # 当前在用的排第一，其余按池序，最多 LINES 条
        alive.sort(key=lambda r: (r["host"] != host, -r.get("kbps", 0)))
        lines = alive[:LINES] or [dict(host=host, mode=mode)]
        out, seen = [], set()
        for c in self.channels:
            mc = c.get("mcast")
            if not mc or mc in seen:
                continue
            seen.add(mc)
            nm = c.get("name") or "未命名"
            for r in lines:
                out.append((f'#EXTINF:-1 group-title="{self.group}",{nm}',
                            f"http://{r['host']}/{r.get('mode', mode)}/{mc}"))
        return out

    def m3u(self):
        host = self.active.get("host") or ""
        mode = self.active.get("mode", "udp")
        lines = ["#EXTM3U"]
        if not host:
            return "\n".join(lines) + "\n"
        lines.append(f"# {self.name} 中转 {host} {datetime.now():%Y-%m-%d %H:%M}")
        seen = set()
        for c in self.channels:
            mc = c.get("mcast")
            if not mc or mc in seen:
                continue
            seen.add(mc)
            g = c.get("group") or self.name
            lines.append(f'#EXTINF:-1 group-title="{g}",{c.get("name") or "未命名"}')
            lines.append(f"http://{host}/{mode}/{mc}")
        return "\n".join(lines) + "\n"

    def snapshot(self):
        with self.lock:
            return dict(name=self.name, key=self.key,
                        active=dict(self.active),
                        active_kbps=next((r.get("kbps", 0) for r in self.relays
                                          if r.get("host") == self.active.get("host")), 0),
                        channels=len(self.channels),
                        relays=[dict(r) for r in self.relays])


def load(path):
    if not os.path.exists(path):
        return []
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [Region(x) for x in (d.get("regions") or []) if x.get("key")]


def save(path, regions):
    # 注意：snapshot() 里的 channels 是「数量」，落盘必须写完整的频道列表，
    # 否则下次 load() 拿到 int，len() 会炸。
    data = {"regions": []}
    for r in regions:
        snap = r.snapshot()
        snap["channels"] = r.channels
        data["regions"].append(snap)
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, path)
