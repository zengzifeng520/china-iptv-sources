# -*- coding: utf-8 -*-
"""直播源有效性检测：并发 HTTP 探测，识别 m3u8 / FLV / TS 流。

三级校验（保证"能播"而不只是"能下载"）：
  master playlist(200) -> 变体 playlist(200) -> 首个 TS 分片(200 且为媒体数据)
直出 TS / FLV / MP4 媒体流则直接判为可用，并把 master 中的首个可用变体
URL（分片清单）作为订阅地址返回，避免播放器解析多码率变体列表失败。
"""
import concurrent.futures as cf
import logging
import socket
import urllib.request
from urllib.parse import urlsplit

from .fetcher import UA

log = logging.getLogger("validator")

# 允许的 Content-Type（直出媒体）
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


def _fetch(url, timeout, want_bytes=8192):
    """GET 并读取最多 want_bytes 字节。返回 (data, content_type) 或 None。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Range": "bytes=0-65535",  # 部分源支持 Range，减小流量
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status not in (200, 206):
            return None
        data = resp.read(want_bytes)
        ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return data, ct


def _abs_url(base, ref):
    """把相对引用解析为绝对 URL。"""
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    if ref.startswith("/"):
        p = urlsplit(base)
        return f"{p.scheme}://{p.netloc}{ref}"
    return base.rsplit("/", 1)[0] + "/" + ref


def _is_media(data, ct):
    """判断数据是否为可直接播放的媒体（TS/FLV/MP4 头或合法 content-type）。"""
    if not data:
        return False
    if data[0] == 0x47 or data.startswith(b"FLV") or data[:4] == b"\x00\x00\x00\x18ftyp":
        return True
    return ct in _OK_CT or ct.startswith("video/") or ct.startswith("audio/")


def _probe_segment(url, timeout):
    """探测单个媒体分片是否可下载且为媒体数据。"""
    got = _fetch(url, timeout, want_bytes=4096)
    if got is None:
        return False
    data, ct = got
    return _is_media(data, ct)


def _probe_variant(vurl, timeout, depth):
    """探测变体 playlist：递归定位到分片清单或直出媒体。返回可播 URL 或 None。"""
    got = _fetch(vurl, timeout)
    if got is None:
        return None
    data, ct = got
    if not data:
        return None
    # 直出媒体
    if data[0] == 0x47 or data.startswith(b"FLV") or data[:4] == b"\x00\x00\x00\x18ftyp":
        return vurl
    head = data[:8192].decode("utf-8", "ignore")
    is_m3u8 = ("m3u8" in vurl.lower()) or ("mpegurl" in ct) or head.lstrip().startswith("#EXTM3U")
    if not is_m3u8:
        return vurl if _is_media(data, ct) else None
    lines = [l.strip() for l in head.splitlines()]
    has_master = any(l.startswith("#EXT-X-STREAM-INF") for l in lines)
    refs = [l for l in lines if l and not l.startswith("#")]
    if not refs:
        return None
    if has_master:
        if depth >= 2:
            return None
        for ref in refs[:2]:
            r = _probe_variant(_abs_url(vurl, ref), timeout, depth + 1)
            if r:
                return r
        return None
    # 分片清单：验证首个分片
    if _probe_segment(_abs_url(vurl, refs[0]), timeout):
        return vurl
    return None


def _probe_best(url, timeout, depth=0):
    """核心三级校验。返回最终可播 URL（master 会下沉到首个可用变体）或 None。"""
    got = _fetch(url, timeout)
    if got is None:
        return None
    data, ct = got
    if not data:
        return None
    # 直出媒体流
    if data[0] == 0x47 or data.startswith(b"FLV") or data[:4] == b"\x00\x00\x00\x18ftyp":
        return url
    head = data[:8192].decode("utf-8", "ignore")
    is_m3u8 = ("m3u8" in url.lower()) or ("mpegurl" in ct) or head.lstrip().startswith("#EXTM3U")
    if not is_m3u8:
        return url if _is_media(data, ct) else None
    lines = [l.strip() for l in head.splitlines()]
    has_master = any(l.startswith("#EXT-X-STREAM-INF") for l in lines)
    refs = [l for l in lines if l and not l.startswith("#")]
    if not refs:
        return None
    if has_master:
        if depth >= 2:
            return None
        for ref in refs[:2]:
            r = _probe_variant(_abs_url(url, ref), timeout, depth + 1)
            if r:
                return r
        return None
    # 直接分片清单
    if _probe_segment(_abs_url(url, refs[0]), timeout):
        return url
    return None


def check_stream_best(url, timeout=8, retries=1):
    """检测直播源并返回最佳可播 URL；不可播返回 None。

    超时（timeout）类失败不重试——重试只会把等待时间翻倍；
    仅对连接被拒、DNS 失败等瞬时错误重试。
    """
    for i in range(retries + 1):
        try:
            return _probe_best(url, timeout)
        except socket.timeout:
            return None
        except TimeoutError:
            return None
        except Exception:  # noqa: BLE001
            if i == retries:
                return None
    return None


def check_stream(url, timeout=8, retries=1):
    """检测单个直播源是否有效（布尔）。"""
    return check_stream_best(url, timeout=timeout, retries=retries) is not None


def check_stream_best_many(urls, timeout=8, workers=40, retries=1):
    """并发检测一批直播源，返回 {url: best_url 或 None}。"""
    result = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_stream_best, u, timeout, retries): u for u in urls}
        for f in cf.as_completed(futs):
            u = futs[f]
            try:
                result[u] = f.result()
            except Exception:  # noqa: BLE001
                result[u] = None
    return result


def check_stream_many(urls, timeout=8, workers=40, retries=1):
    """并发检测一批直播源，返回 {url: bool}（兼容旧接口）。"""
    best = check_stream_best_many(urls, timeout=timeout, workers=workers, retries=retries)
    return {u: (b is not None) for u, b in best.items()}
