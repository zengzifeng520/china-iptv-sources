#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""speed_rank.py —— 按实测速度给源排序，生成"快速版"订阅 output/fast.m3u

背景：晚上 8~11 点，公共 udpxy 网关普遍被限速到 0.5 Mbps 左右（实测中位数 0.50，
只有 5% 能跑到 2 Mbps 以上），排序没意义，得按"当下能不能跑"来筛。

用法：
    python3 speed_rank.py                 # 默认：output/checked/live.m3u -> output/fast.m3u
    python3 speed_rank.py --min 1.5       # 只保留 >= 1.5 Mbps
    python3 speed_rank.py --no-push       # 只生成本地文件
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

UA = "okhttp/3.12.0"


def fetch_speed(url, secs=3.0, limit=4 * 1024 * 1024):
    """返回 (mbps, 采样字节)；m3u8 会先取分片再测。"""
    t0 = time.time()
    n = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            head = r.read(4096)
            n += len(head)
            if b"#EXTM3U" in head or url.lower().split("?")[0].endswith(".m3u8"):
                text = head.decode("utf-8", "replace")
                seg = ""
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        seg = line
                        break
                if seg:
                    seg_url = urljoin(url, seg)
                    n = 0
                    t0 = time.time()
                    req2 = urllib.request.Request(seg_url, headers={"User-Agent": UA})
                    with urllib.request.urlopen(req2, timeout=6) as r2:
                        while time.time() - t0 < secs and n < limit:
                            b = r2.read(65536)
                            if not b:
                                break
                            n += len(b)
            else:
                while time.time() - t0 < secs and n < limit:
                    b = r.read(65536)
                    if not b:
                        break
                    n += len(b)
    except Exception:  # noqa: BLE001
        pass
    dt = max(0.2, time.time() - t0)
    return n * 8 / dt / 1e6, n


def parse_blocks(path):
    """返回 (header_lines, [(extinf, url)])"""
    header, blocks, pending = [], [], None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            if s.startswith("#EXTINF"):
                if pending:
                    blocks.append((pending, None))
                pending = s
            elif s.startswith("#"):
                if pending is None:
                    header.append(s)
            else:
                blocks.append((pending, s))
                pending = None
    if pending:
        blocks.append((pending, None))
    return header, blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(BASE, "output", "checked", "live.m3u"))
    ap.add_argument("--out", default=os.path.join(BASE, "output", "fast.m3u"))
    ap.add_argument("--min", type=float, default=1.0, help="保留阈值 Mbps")
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--secs", type=float, default=3.0)
    ap.add_argument("--dedupe", action="store_true", help="同名频道只保留最快的若干条")
    ap.add_argument("--keep-per-name", type=int, default=2, help="去重时每个频道保留几条")
    ap.add_argument("--limit", type=int, default=0, help="最后只保留前 N 条（0=不限）")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    header, blocks = parse_blocks(args.src)
    urls = [u for _e, u in blocks if u and u.lower().startswith(("http://", "https://"))]
    print("解析 %d 条，可测速 %d 条，并发 %d，单条 %.1fs"
          % (len(blocks), len(urls), args.workers, args.secs), flush=True)

    speed = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_speed, u, args.secs): u for u in urls}
        done = 0
        for f in as_completed(futs):
            u = futs[f]
            try:
                speed[u] = f.result()[0]
            except Exception:  # noqa: BLE001
                speed[u] = 0.0
            done += 1
            if done % 500 == 0:
                print("  进度 %d/%d  已用 %.0fs" % (done, len(urls), time.time() - t0), flush=True)

    rank = {}
    for _e, u in blocks:
        if u:
            rank[u] = speed.get(u, 0.0 if u.startswith("http") else 99.0)

    kept = [(e, u) for e, u in blocks if u and (not u.startswith("http") or rank.get(u, 0) >= args.min)]
    kept.sort(key=lambda x: -rank.get(x[1], 0))
    if args.dedupe:
        seen, dedup = {}, []
        for e, u in kept:
            nm = (e or "").rsplit(",", 1)[-1].strip() or u
            n = seen.get(nm, 0)
            if n >= max(1, args.keep_per_name):
                continue
            seen[nm] = n + 1
            dedup.append((e, u))
        kept = dedup

    if args.limit and len(kept) > args.limit:
        kept = kept[:args.limit]
        print("只保留最快的 %d 条" % args.limit)

    out = [header[0] if header else "#EXTM3U"]
    if len(header) > 1:
        out.extend(header[1:])
    for e, u in kept:
        if e:
            out.append(e)
        out.append(u)
    text = "\n".join(out) + "\n"
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    vals = sorted((v for v in speed.values()), reverse=True)
    if vals:
        med = vals[len(vals) // 2]
        print("测速完成：中位 %.2f Mbps，最快 %.2f Mbps" % (med, vals[0]))
    print("保留 %d 条（>= %.1f Mbps）-> %s" % (len(kept), args.min, args.out))

    if len(kept) < 1:
        print("保留下来的太少（%d 条），不覆盖上一版，避免订阅被清空" % len(kept))
        return 0

    if not args.no_push and kept:
        try:
            import push_nas as P
            token = P.load_token()
            repo = os.environ.get("GITHUB_REPO", P.DEFAULT_REPO)
            P.push_file(repo, "output/fast.m3u", text.encode("utf-8"), token)
            print("已推送 output/fast.m3u")
            print("订阅地址：https://zengzifeng.ccwu.cc/output/fast.m3u")
        except Exception as exc:  # noqa: BLE001
            print("推送失败:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
