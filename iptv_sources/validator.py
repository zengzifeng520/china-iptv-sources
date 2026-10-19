# -*- coding: utf-8 -*-
"""直播源有效性检测：并发 HTTP 探测，识别 m3u8 / FLV / TS 流。"""
import concurrent.futures as cf
import logging
import socket
import urllib.request

from .fetcher import UA

log = logging.getLogger("validator")

# 允许的 Content-Type（精确匹配）
_OK_CT = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "audio/x-mpegurl",
    "video/mp2t",
    "video/x-flv",
    "video/flv",
    "video/mp4",
    "application/octet-stream",
    "binary/octet-stream",
}


def _probe_once(url, timeout):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Range": "bytes=0-65535",  # 部分源支持 Range，减小流量
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            return False
        body = resp.read(65536)
        ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        low = url.lower()
        is_m3u8 = "m3u8" in low or "mpegurl" in ct
        if is_m3u8:
            # 真正的 m3u8 播放列表必须以 #EXTM3U 或包含 #EXT-X- 标签开头
            head = body[:8192].decode("utf-8", "ignore")
            return "#EXTM3U" in head or "#EXT-X-" in head
        if body.startswith(b"FLV"):
            return True
        if body and body[0] == 0x47:  # TS 流同步字节
            return True
        return ct in _OK_CT or ct.startswith("video/") or ct.startswith("audio/")


def check_stream(url, timeout=8, retries=1):
    """检测单个直播源是否有效。

    超时（timeout）类失败不重试——重试只会把等待时间翻倍，对超时源没有意义；
    仅对连接被拒、DNS 失败等瞬时错误重试。
    """
    for i in range(retries + 1):
        try:
            return _probe_once(url, timeout)
        except socket.timeout:
            return False
        except TimeoutError:
            return False
        except Exception:  # noqa: BLE001
            if i == retries:
                return False
    return False


def check_stream_many(urls, timeout=8, workers=40, retries=1):
    """并发检测一批直播源。

    Returns:
        {url: bool}
    """
    result = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_stream, u, timeout, retries): u for u in urls}
        for f in cf.as_completed(futs):
            u = futs[f]
            try:
                result[u] = f.result()
            except Exception:  # noqa: BLE001
                result[u] = False
    return result
