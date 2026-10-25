#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 IPTVZB 的巡检列表（checked）同步成每个用户的专属订阅文件。

流程：拉取 https://zzf.new.tg/live.m3u → 逐个用户推到 output/users/u_<token>.m3u
      → 过期用户删除文件 → 清理孤儿文件。
由 NAS cron 在 05:45 / 15:45 调用（IPTVZB 的 05:05 / 15:05 任务跑完后）。
"""
import json
import os
import re
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from push_nas import (DEFAULT_REPO, load_token, push_file, delete_file,  # noqa: E402
                      push_files_batch)

USERS_FILE = os.path.join(BASE, "users.json")
SOURCE = os.environ.get("USER_SYNC_SOURCE", "https://zzf.new.tg/checked/live.m3u")
FALLBACK = "https://cdn.jsdelivr.net/gh/zengzifeng520/IPTVZB@main/checked/live.m3u"
FALLBACK2 = "https://zzf.new.tg/live.m3u"
MIN_BYTES = 100 * 1024
# 访问心跳服务（NAS 上的 track.py）；用公网地址，播放器才请求得到
PEAK_START = int(os.environ.get("PEAK_START", "19"))
PEAK_END = int(os.environ.get("PEAK_END", "23"))
PEAK_MAX_AGE = int(os.environ.get("PEAK_MAX_AGE", "10800"))
TRACK_BASE = os.environ.get("TRACK_BASE", "http://116.230.212.78:8898")
EPG_REAL = os.environ.get("EPG_REAL", "https://zengzifeng.ccwu.cc/output/epg.xml")

_FALLBACK_BASE = "http://116.230.212.78:8898"


def public_base(port=8898):
    """心跳地址：TRACK_BASE 配死就照用；为 auto 时实时取公网 IPv4。"""
    base = os.environ.get("TRACK_BASE", "")
    if base and base != "auto":
        return base.rstrip("/")
    try:
        with open(os.path.join(BASE, "tunnel_url.txt"), encoding="utf-8") as f:
            u = f.read().strip()
        if u.startswith("http"):
            return u.rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
            ip = r.read().decode("utf-8", "replace").strip()
        if ip.count(".") == 3:
            return "http://%s:%d" % (ip, port)
    except Exception:  # noqa: BLE001
        pass
    return _FALLBACK_BASE


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nas-user-sync/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _url_rank(u):
    ul = (u or "").lower()
    if "m3u8" in ul:
        return 0
    if "/rtp/" in ul or "/udp/" in ul:
        return 2
    return 1


def reorder_cdn_first(lines):
    """CDN(m3u8) 排最前，普通 http 居中，rtp/udp 公共网关垫底。"""
    head, blocks, cur, seen = [], [], [], False
    for ln in lines:
        if ln.startswith("#EXTINF"):
            seen = True
            if cur:
                blocks.append(cur)
            cur = [ln]
        elif not seen:
            head.append(ln)
        elif ln.startswith("#"):
            if cur:
                cur.append(ln)
        elif ln.strip():
            cur.append(ln)
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    def url_of(b):
        for x in b:
            if x and not x.startswith("#"):
                return x.strip()
        return ""

    blocks.sort(key=lambda b: _url_rank(url_of(b)))
    out = list(head)
    for b in blocks:
        out.extend(b)
    return out


def reorganize_netlive(blocks):
    """网络直播源：同名去重合并成多线路，组名保持「网络直播」。"""
    net, rest = [], []
    for e, u in blocks:
        (net if 'group-title="网络直播"' in e else rest).append((e, u))
    if not net:
        return blocks
    merged, order = {}, []
    for e, u in net:
        name = e.rsplit(",", 1)[-1].strip()
        if not name:
            rest.append((e, u))
            continue
        if name not in merged:
            merged[name] = []
            order.append(name)
        merged[name].append(u)
    out = []
    for name in order:
        for u in merged[name]:
            out.append((f'#EXTINF:-1 group-title="网络直播",{name}', u))
    return rest + out


NL_CAT = {"央视": 0, "卫视": 1, "广东": 2, "深圳": 3, "地区": 4}


def _nl_cat(name):
    u = name.upper()
    if "CCTV" in u:
        return "央视"
    if "深圳" in name:
        return "深圳"
    if "广东" in name or name.startswith("珠江") or name.startswith("南方"):
        return "广东"
    if "卫视" in name:
        return "卫视"
    return "地区"


def reorder_netlive_run(blocks):
    """把连续的「网络直播」块按 央视→卫视→广东→深圳→地区（组内再按名字）重排。"""
    out, i, n = [], 0, len(blocks)
    while i < n:
        if 'group-title="网络直播"' in blocks[i][0]:
            j = i
            while j < n and 'group-title="网络直播"' in blocks[j][0]:
                j += 1
            run = sorted(blocks[i:j],
                         key=lambda p: (NL_CAT.get(_nl_cat(p[0].rsplit(",", 1)[-1].strip()), 9),
                                        p[0].rsplit(",", 1)[-1].strip()))
            out += run
            i = j
        else:
            out.append(blocks[i])
            i += 1
    return out


def build_for_user(expire_date, days, created="", token="", track_base="", extra_blocks=None):
    """生成用户专属订阅：头部 + 订阅信息 + 全部频道（全部来自巡检列表 extra_sources.json）。"""
    head = "#EXTM3U"
    logo_attr = ""
    if token and track_base:
        base = track_base.rstrip("/")
        epg_track = f"{base}/epg/{token}.xml"
        logo_track = f"{base}/logo/{token}.png"
        logo_attr = f' tvg-logo="{logo_track}"'
        # x-tvg-url 走心跳（计数）；url-tvg 保留真实地址做兜底，NAS 挂了下也能拿到节目单
        head = f'#EXTM3U x-tvg-url="{epg_track}" url-tvg="{EPG_REAL}"'
    group = f"订阅信息 到期 {expire_date}"
    name = (f"有效期 {created} ~ {expire_date}（剩余 {days} 天）" if created
            else f"订阅有效至 {expire_date}（剩余 {days} 天）")
    first_url = ""
    for _e, u in (extra_blocks or []):
        if u and not u.startswith("#"):
            first_url = u
            break
    block = [
        f'#EXTINF:-1 tvg-id="订阅" tvg-name="订阅" group-title="{group}"{logo_attr},{name}',
        first_url or "",
    ]
    out_lines = [head] + block
    # 推送时间分组：每次推送都带，排在"更新时间"后（位置由 group_order.txt 决定）
    import datetime as _dt
    push_time = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    push_extinf = f'#EXTINF:-1 tvg-id="推送" tvg-name="推送" group-title="推送时间 {push_time}",{push_time} 推送'
    push_pair = (push_extinf, first_url or "")
    # 按 group_order.txt 统一分组顺序（央视频道/卫视频道/地区频道置顶），组内按频道名自然排序
    try:
        from group_sort import sort_pairs
        _blocks = sort_pairs([push_pair] + reorganize_netlive(list(extra_blocks or [])))
    except Exception as exc:  # noqa: BLE001
        print("分组排序跳过：%s" % exc, flush=True)
        _blocks = [push_pair] + list(extra_blocks or [])
    # 丢弃「更新时间」分组（IPTVZB 过期占位），只保留我们自己注入的「推送时间」
    _blocks = [b for b in _blocks if 'group-title="更新时间' not in b[0]]
    _blocks = reorder_netlive_run(_blocks)
    # 多源叠加后同一频道可能有几十条，全局按频道名封顶
    _top = int(os.environ.get("TOP_PER_CHANNEL", "20"))
    _cnt, _cap = {}, []
    for _e, _u in _blocks:
        _n = _e.rsplit(",", 1)[-1].strip()
        if _cnt.get(_n, 0) >= _top:
            continue
        _cnt[_n] = _cnt.get(_n, 0) + 1
        _cap.append((_e, _u))
    _blocks = _cap
    for e, u in _blocks:
        out_lines.append(e)
        out_lines.append(u)
    return ("\n".join(out_lines) + "\n").encode("utf-8")


def main():
    users = {}
    if os.path.exists(USERS_FILE):
        try:
            users = json.load(open(USERS_FILE, encoding="utf-8"))
        except Exception:
            users = {}

    # 只从巡检列表（extra_sources.json）拉取订阅，取消单独的主订阅 SOURCE
    extra_blocks = []
    try:
        from extra_source import get_extra_blocks
        extra_blocks = get_extra_blocks()
    except Exception as exc:  # noqa: BLE001
        print('外部源载入失败: %s' % exc, flush=True)

    if not extra_blocks:
        print("巡检列表为空，放弃本轮", flush=True)
        return 1

    n = len(extra_blocks)
    print("订阅模式：仅巡检列表，共 %d 条" % n, flush=True)

    token = load_token()
    now = int(time.time())
    wanted = set()
    active = 0
    batch = []
    print("用户数: %d" % len(users), flush=True)

    for name, info in sorted(users.items()):
        tok = str(info.get("token", "") or "")
        exp = int(str(info.get("ts", "0") or 0))
        if not tok:
            continue
        fname = "u_%s.m3u" % tok
        if exp and exp < now:
            try:
                if delete_file(DEFAULT_REPO, "output/users/" + fname, token):
                    print("  [%s] 已过期，删除 %s" % (name, fname), flush=True)
            except Exception as e:  # noqa: BLE001
                print("  [%s] 删除失败: %s" % (name, e), flush=True)
            continue
        expire_date = str(info.get("expire", "") or "")
        days = max(0, (exp - now) // 86400) if exp else 0
        payload = build_for_user(expire_date, days,
                                 str(info.get("created", "") or ""), tok, public_base(), extra_blocks)
        batch.append(("output/users/" + fname, payload))
        wanted.add(fname)
        active += 1
        print("  [%s] 已生成 %s" % (name, fname), flush=True)

    # 7 个用户文件合并成一次提交：避免逐个 push 触发多次 Pages 构建
    if batch:
        try:
            n = push_files_batch(DEFAULT_REPO, batch, token,
                                 message="chore: 更新用户订阅（%d 个）" % len(batch))
            if n:
                print("  批量提交：%d 个文件有变化，合并为 1 次提交" % n, flush=True)
            else:
                print("  内容无变化，跳过提交", flush=True)
        except Exception as e:  # noqa: BLE001
            print("  批量提交失败: %s" % e, flush=True)

    # 清理孤儿
    try:
        url = "https://api.github.com/repos/%s/contents/output/users" % DEFAULT_REPO
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                   "User-Agent": "nas-user-sync"})
        with urllib.request.urlopen(req, timeout=30) as r:
            items = json.loads(r.read().decode("utf-8"))
        for it in items:
            fname = it.get("name", "")
            if fname.endswith(".m3u") and fname not in wanted:
                try:
                    delete_file(DEFAULT_REPO, "output/users/" + fname, token)
                    print("  [清理] 删除孤儿 %s" % fname, flush=True)
                except Exception:
                    pass
    except Exception:
        pass

    print("同步完成：生效 %d 个用户" % active, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
