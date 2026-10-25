# -*- coding: utf-8 -*-
"""
把某地区的组播列表转成你自己路由器可播放的单播地址。

用法：
  python make_proxy.py 广东-电信 192.168.1.1:5140 udp
  python make_proxy.py 广东-电信 192.168.1.1:4022 rtp
  python make_proxy.py --list

参数3：udp = rtp2httpd / msd_lite 风格；rtp = udpxy 风格
生成文件：output/play-<地区-运营商>-<宿主>.m3u
"""
import os, sys, re

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
MDIR = os.path.join(OUT, "multicast")

def list_lines():
    names = sorted({os.path.splitext(f)[0] for f in os.listdir(MDIR) if f.endswith(".txt")})
    print("可选线路：")
    for i, n in enumerate(names, 1):
        print(f"  {i:2d}. {n}")
    return names

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-l", "--list"):
        list_lines()
        return
    line = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else "192.168.1.1:5140"
    kind = (sys.argv[3] if len(sys.argv) > 3 else "udp").lower()
    if kind not in ("udp", "rtp"):
        print("第3个参数只能是 udp 或 rtp")
        return

    src = None
    for f in os.listdir(MDIR):
        if f.endswith(".txt") and os.path.splitext(f)[0] == line:
            src = os.path.join(MDIR, f)
    if not src:
        print(f"没找到「{line}」，用 --list 看可用线路")
        return

    out = ["#EXTM3U", f"# {line} -> {host} ({kind})"]
    n = 0
    for raw in open(src, encoding="utf-8"):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "," not in s:
            continue
        name, url = s.split(",", 1)
        m = re.search(r"rtp://(\d+\.\d+\.\d+\.\d+):(\d+)", url)
        if not m:
            continue
        out.append(f'#EXTINF:-1 group-title="{line}",{name}')
        out.append(f"http://{host}/{kind}/{m.group(1)}:{m.group(2)}")
        n += 1

    dst = os.path.join(OUT, f"play-{line}-{host.replace(':','_')}.m3u")
    open(dst, "w", encoding="utf-8").write("\n".join(out))
    print(f"已生成 {n} 个频道 -> {dst}")
    print(f"示例：http://{host}/{kind}/" + re.search(r"rtp://([\d.]+:\d+)", open(src, encoding="utf-8").read()).group(1))

if __name__ == "__main__":
    main()
