"""
额外组播源（第三方 txt 列表）— 解析后并入 config.mcast_extra["group"] 指定的分类

源地址带密钥，不能写进公开仓库，按下面顺序读取：
    1. 环境变量 CQSHUSHU_URL（GitHub Actions 用 secrets 注入）
    2. crawl/.cq_url 文件（本地用，已 gitignore）

源站对非浏览器 UA 返回 403，所以固定带浏览器 UA + Referer。
"""
import asyncio
import logging
import os
import re
import urllib.request

import config

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_RTP = re.compile(r"https?://[^\s,]+?/(rtp|udp)/(\d+\.\d+\.\d+\.\d+:\d+)")


def _cfg():
    return getattr(config, "mcast_extra", {}) or {}


def _source_url():
    cfg = _cfg()
    url = (os.environ.get(cfg.get("url_env", "CQSHUSHU_URL")) or "").strip()
    if url:
        return url
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        cfg.get("url_file", ".cq_url"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _fetch_sync(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": _cfg().get("referer", "https://iptv.cqshushu.com/"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_txt(text):
    """解析 '频道名,http://ip:port/rtp/239.x.x.x:port' 形式的 txt 列表"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "," not in line:
            continue
        name, url = line.split(",", 1)
        name, url = name.strip(), url.strip()
        if not name or not _RTP.search(url):
            continue
        out.setdefault(name, [])
        if url not in out[name]:
            out[name].append(url)
    return out


async def fetch_extra_multicast():
    """返回 {category: {name: [urls]}}，失败时返回 {}（不影响主流程）"""
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return {}
    url = _source_url()
    if not url:
        logger.info("[附加源] 未配置地址（环境变量或 .cq_url），跳过")
        return {}

    timeout = float(cfg.get("timeout", 30))
    try:
        text = await asyncio.to_thread(_fetch_sync, url, timeout)
    except Exception as e:
        logger.warning(f"[附加源] 拉取失败: {e}")
        return {}

    channels = parse_txt(text)
    if not channels:
        logger.warning("[附加源] 解析结果为空，跳过")
        return {}

    cat = cfg.get("group") or "组播源-附加源"
    total = sum(len(v) for v in channels.values())
    logger.info(f"[附加源] {cat} 解析到 {len(channels)} 个频道、{total} 条线路")
    return {cat: channels}
