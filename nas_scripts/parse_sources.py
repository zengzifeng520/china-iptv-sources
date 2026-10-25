# -*- coding: utf-8 -*-
"""解析 spider-iptv 仓库中的各地区组播源，统一成结构化数据。"""
import os, re, json, glob, collections

BASE = r"E:\iptv\spider\spider-iptv-main\source"
OUT = r"E:\iptv\output"

MCAST = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})")
# http://host:port/rtp|udp/<mcast>:<port>
PROXY = re.compile(r"(https?://[^/,\s]+)/(rtp|udp)/+(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})")
RTP = re.compile(r"rtp://(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})")

def parse_file(path):
    """返回 [(name, mcast, proxy_url, kind)]"""
    raw = open(path, encoding="utf-8-sig", errors="ignore").read()
    if "<html" in raw.lower() or "<!doctype" in raw.lower():
        return None  # 错误页
    rows = []
    extinf_name = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r",(.+?)\s*$", line)
            extinf_name = m.group(1).strip() if m else None
            continue
        if line.startswith("#"):
            continue
        # 一行可能是 name,url
        name = None
        url = line
        if "," in line and "://" in line:
            head, tail = line.split(",", 1)
            if "://" in tail:
                name, url = head.strip(), tail.strip()
        if name is None:
            name = extinf_name or ""
        extinf_name = None

        pm = PROXY.search(url)
        rm = RTP.search(url)
        if pm:
            host = pm.group(1); mc = f"{pm.group(3)}:{pm.group(4)}"
            rows.append((name, mc, f"{host}/{pm.group(2)}/{mc}", pm.group(2)))
        elif rm:
            mc = f"{rm.group(1)}:{rm.group(2)}"
            rows.append((name, mc, "", "rtp"))
        else:
            # 纯 mcast:port
            m = MCAST.search(url)
            if m and "://" not in url:
                rows.append((name, f"{m.group(1)}:{m.group(2)}", "", "raw"))
    return rows

def main():
    os.makedirs(OUT, exist_ok=True)
    result = {}
    skipped = []
    for sub in ("multicast", "download", "hotels"):
        for path in sorted(glob.glob(os.path.join(BASE, sub, "*.txt")) +
                           glob.glob(os.path.join(BASE, sub, "*.m3u"))):
            fn = os.path.splitext(os.path.basename(path))[0]
            if fn in result:  # txt 与 m3u 内容重复，取先到的 txt
                continue
            rows = parse_file(path)
            if rows is None:
                skipped.append((fn, "HTML错误页"))
                continue
            if not rows:
                skipped.append((fn, "无有效条目"))
                continue
            # 去重
            seen, uniq = set(), []
            for r in rows:
                key = (r[1], r[0])
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(r)
            parts = fn.split("-")
            region = parts[0]
            isp = parts[1] if len(parts) > 1 else ""
            seg = parts[2] if len(parts) > 2 else ""
            result[fn] = dict(file=fn, region=region, isp=isp, segment=seg,
                              source_type=sub, count=len(uniq),
                              entries=[{"name": n, "mcast": m, "proxy": p, "kind": k}
                                       for n, m, p, k in uniq])
    json.dump(result, open(os.path.join(OUT, "channels.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print(f"{'地区-运营商':<22}{'组播段':<18}{'频道数':>6}  接口类型")
    print("-" * 70)
    for k, v in sorted(result.items(), key=lambda x: (x[1]['region'], x[1]['isp'])):
        kinds = collections.Counter(e["kind"] for e in v["entries"])
        kt = "/".join(f"{a}:{b}" for a, b in kinds.items())
        print(f"{v['region']+'-'+v['isp']:<22}{v['segment']:<18}{v['count']:>6}  {kt}")
    print("-" * 70)
    print("合计", sum(v["count"] for v in result.values()), "条  共", len(result), "个地区线路")
    if skipped:
        print("\n跳过:", ", ".join(f"{a}({b})" for a, b in skipped))

if __name__ == "__main__":
    main()
