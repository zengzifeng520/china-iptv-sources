# -*- coding: utf-8 -*-
"""直播源文本解析：支持 TVBox TXT（#genre# 分类格式）与 M3U/M3U8 两种格式。"""
import re

URL_PREFIXES = ("http://", "https://", "rtmp://", "rtsp://", "udp://", "rtp://", "http-get://")


def looks_like_url(u):
    return u.startswith(URL_PREFIXES)


def parse_txt(text):
    """解析 TVBox 格式的 TXT。

    分类行格式:  `#央视频道,#genre#`
    条目行格式:  `频道名,url`
    """
    entries = []
    category = "其他"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(",#genre#"):
            category = line[: -len(",#genre#")].lstrip("#").strip() or "其他"
            continue
        if line.startswith("#"):  # 其它注释行
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        name = name.strip()
        url = url.strip()
        if name and looks_like_url(url):
            entries.append((category, name, url))
    return entries


def parse_m3u(text):
    """解析 M3U/M3U8。

    EXTINF 行格式:  `#EXTINF:-1 group-title="分类",频道名`
    其后一行是 url。
    """
    entries = []
    category = "其他"
    pending_name = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            if m and m.group(1):
                category = m.group(1).strip()
            pending_name = line.rsplit(",", 1)[-1].strip() if "," in line else None
            continue
        if line.startswith("#"):  # 其它注释行
            continue
        if pending_name:
            if looks_like_url(line):
                entries.append((category, pending_name, line))
            pending_name = None
            continue
        # 没有 EXTINF 的行，按 TXT 规则兜底解析
        if "," in line:
            name, url = line.split(",", 1)
            name = name.strip()
            url = url.strip()
            if name and looks_like_url(url):
                entries.append((category, name, url))
    return entries


def parse_auto(text):
    """自动识别格式并解析。"""
    if text.lstrip().startswith("#EXTM3U"):
        return parse_m3u(text)
    return parse_txt(text)
