# -*- coding: utf-8 -*-
"""HTTP 抓取工具：带 User-Agent、gzip/deflate 解压、多镜像地址自动回退。

纯标准库实现，无第三方依赖，可在 GitHub Actions 与本地直接运行。

自动回退策略：
1. 按 config 中配置的主地址与镜像地址依次尝试；
2. 若全部失败，且其中存在 raw.githubusercontent.com 地址，则自动派生
   api.github.com contents API 地址再尝试（该域名在国内通常可直连，
   可绕过 raw 域名被封锁的问题）。
"""
import base64
import gzip
import json
import logging
import re
import urllib.parse
import urllib.request
import zlib

log = logging.getLogger("fetcher")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_RAW_RE = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$")


def _github_api_fallback_url(raw_url):
    """把 raw.githubusercontent.com 地址转为 api.github.com contents API 地址。"""
    m = _RAW_RE.match(raw_url)
    if not m:
        return None
    owner, repo, ref, path = m.groups()
    quoted = urllib.parse.quote(path)
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{quoted}?ref={ref}"


def _safe_url(url):
    """对 URL 路径中的非 ASCII 字符做百分号编码（urllib 不支持原始中文路径）。"""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _open(url, timeout):
    req = urllib.request.Request(
        _safe_url(url),
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _decompress(data, resp):
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        return gzip.decompress(data)
    if enc == "deflate":
        return zlib.decompress(data)
    return data


def fetch_bytes(url, timeout=15, max_bytes=None):
    """抓取二进制内容；若服务器返回 gzip/deflate 压缩则自动解压。"""
    with _open(url, timeout) as resp:
        data = resp.read(max_bytes) if max_bytes else resp.read()
        return _decompress(data, resp)


def fetch_text(url, timeout=15, max_bytes=None, encoding="utf-8"):
    return fetch_bytes(url, timeout, max_bytes).decode(encoding, errors="replace")


def _fetch_github_api_text(url, timeout=15):
    """抓取 api.github.com contents API 返回的 JSON 并解出 base64 内容。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _decompress(resp.read(), resp)
    obj = json.loads(data.decode("utf-8"))
    if isinstance(obj, dict) and obj.get("encoding") == "base64":
        return base64.b64decode(obj["content"]).decode("utf-8", "replace")
    raise RuntimeError("api.github.com 返回了意外的响应结构")


def fetch_text_with_fallback(name, urls, timeout=15, max_bytes=None, encoding="utf-8"):
    """依次尝试多个镜像地址，返回第一个成功抓取的文本。

    Args:
        name: 数据源名称（仅用于日志）。
        urls: 候选地址列表，按优先级排列（主源在前、镜像在后）。
    Returns:
        抓取到的文本内容。
    Raises:
        RuntimeError: 所有地址均失败。
    """
    # 自动追加 GitHub API 备用通道
    candidates = list(urls)
    for u in urls:
        fb = _github_api_fallback_url(u)
        if fb and fb not in candidates:
            candidates.append(fb)

    last_err = None
    for u in candidates:
        try:
            if "api.github.com/repos/" in u:
                text = _fetch_github_api_text(u, timeout=timeout)
            else:
                text = fetch_text(u, timeout=timeout, max_bytes=max_bytes, encoding=encoding)
            log.info("源[%s] 抓取成功: %s (%d 字符)", name, u, len(text))
            return text
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("源[%s] 抓取失败: %s (%s)", name, u, e)
    raise RuntimeError(f"源[{name}] 全部 {len(candidates)} 个地址均失败: {last_err}")
