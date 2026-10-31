"""
组播源抓取模块（备用链路）— 360 Quake 测绘 + udpxy 转单播

链路：
    1) 用 360 Quake 搜索接口按「省份 + 运营商」检索暴露在公网的 udpxy 服务
       POST https://quake.360.net/api/v3/search/quake_service
       Header: X-QuakeToken: <token>
    2) GET http://{ip}:{port}/status 校验 udpxy 是否存活
    3) 把组播地址库里的 /rtp/239.x.x.x:port 路径拼到新 udpxy 上
       http://{ip}:{port}/rtp/239.x.x.x:port

组播地址库默认取 maowei1125/spider-iptv（taoiptv.com 同源）的 source/multicast/*.m3u，
索引来自仓库里的 data/iptv_data.sql（含每个省份-运营商对应的地址库文件路径）。

当主接口（api.809899.xyz）拿不到数据时，优先复用上一次成功结果的 /rtp/ 路径（快照），
没有快照才去下载地址库，避免每次都拉几十个文件。
"""
import json
import logging
import os
import re
import time
import urllib.parse

import asyncio
import aiohttp

import config

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache", "quake")

_ISP_KEYS = ("电信", "联通", "移动", "广电")
_PROVINCE_STRIP = ("维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市")

# 224.0.0.0/4 组播段（239.x / 238.x / 228.x / 225.x ...）
_RTP_RE = re.compile(r"(?P<kind>/rtp/|/udp/|rtp://|udp://|igmp://)\s*"
                     r"(?P<addr>(?:22[4-9]|23[0-9])\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5})", re.I)
_NAME_RE = re.compile(r",\s*([^,]*?)\s*$")


# ── 配置读取 ─────────────────────────────────────────────────────────
def _cfg():
    return getattr(config, "mcast_quake", {}) or {}


def _enabled():
    c = _cfg()
    return bool(c.get("enabled")) and bool(c.get("token"))


def _timeout():
    return float(_cfg().get("timeout", 10) or 10)


def _cache_days():
    return float(_cfg().get("cache_days", 7) or 7)


def _cache_path(*parts):
    p = os.path.join(CACHE_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _fresh(path):
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < _cache_days() * 86400


def _cdn(url):
    """给 GitHub raw 地址套一层加速前缀（配置为空则原样返回）"""
    prefix = (_cfg().get("cdn_prefix") or "").strip()
    if prefix and url.startswith("https://raw.githubusercontent.com/"):
        return prefix.rstrip("/") + "/" + url
    return url


# ── 地区解析 ─────────────────────────────────────────────────────────
def parse_region(region: str):
    """'安徽省 安徽电信' / '组播源-广西电信' / '广西-电信' → ('广西', '电信')"""
    if not region:
        return "", ""
    text = region.replace("-", " ").replace("_", " ").strip()
    if text.startswith("组播源"):
        text = text[len("组播源"):].strip(" -")
    isp = next((k for k in _ISP_KEYS if k in text), "")
    tokens = text.split()
    province = ""
    for tok in tokens:
        tok = tok.replace(isp, "").strip() if isp else tok
        for suffix in _PROVINCE_STRIP:
            if tok.endswith(suffix):
                tok = tok[: -len(suffix)]
                break
        if tok and not any(k in tok for k in _ISP_KEYS):
            province = tok
            break
    if not province:
        head = text.split(isp)[0] if isp else text
        for suffix in _PROVINCE_STRIP:
            head = head.replace(suffix, "")
        province = head.strip()
    return province, isp


def region_label(province: str, isp: str) -> str:
    return f"组播源-{province}{isp}" if province or isp else "组播源"


# ── 组播地址库 ───────────────────────────────────────────────────────
async def _download(session, url, path, timeout):
    try:
        async with session.get(_cdn(url), timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"[Quake组播] 下载失败 {resp.status}: {url}")
                return None
            data = await resp.read()
        with open(path, "wb") as f:
            f.write(data)
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[Quake组播] 下载异常 {url}: {e}")
        return None


async def load_library_index(session, timeout):
    """解析 iptv_data.sql，得到 {(省份, 运营商): [地址库路径]}"""
    path = _cache_path("library_index.json")
    if _fresh(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {(k.split("|")[0], k.split("|")[1]): v for k, v in raw.items()}
        except Exception:
            pass

    sql_url = _cfg().get("library_index", "")
    if not sql_url:
        return {}
    text = await _download(session, sql_url, _cache_path("iptv_data.sql"), timeout)
    if not text:
        return {}

    index = {}
    for line in text.splitlines():
        if "iptv_multicast" not in line or "INSERT" not in line.upper():
            continue
        parts = re.findall(r"'([^']*)'", line)
        if len(parts) < 6:
            continue
        province, isp, lib_path = parts[1].strip(), parts[3].strip(), parts[5].strip()
        if not lib_path.lower().endswith((".m3u", ".txt")):
            continue
        index.setdefault((province, isp), []).append(lib_path)

    with open(path, "w", encoding="utf-8") as f:
        json.dump({f"{p}|{i}": v for (p, i), v in index.items()}, f, ensure_ascii=False)
    logger.info(f"[Quake组播] 地址库索引 {len(index)} 个省份-运营商组合")
    return index


def parse_library(text: str):
    """m3u/txt 地址库 → {频道名: ['/rtp/239.x.x.x:port', ...]}"""
    out = {}
    name = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            m = _NAME_RE.search(line)
            name = (m.group(1) if m else "").strip()
            if "tvg-name=" in line and not name:
                name = re.search(r'tvg-name="([^"]*)"', line).group(1).strip()
            continue
        if line.startswith("#"):
            continue
        m = _RTP_RE.search(line)
        if not m:
            continue
        addr = m.group("addr")
        kind = "/udp/" if "udp" in m.group("kind").lower() else "/rtp/"
        chan = name or addr
        out.setdefault(chan, [])
        path = kind + addr
        if path not in out[chan]:
            out[chan].append(path)
    return out


async def load_region_channels(session, province, isp, timeout):
    """下载并解析指定省份-运营商的地址库"""
    index = await load_library_index(session, timeout)
    paths = index.get((province, isp)) or []
    if not paths:
        # 兼容索引里省份带「省」或运营商为空的情况
        for (p, i), v in index.items():
            if p.startswith(province) and (not isp or i == isp):
                paths = v
                break
    if not paths:
        logger.info(f"[Quake组播] 地址库没有 {province}-{isp}，跳过")
        return {}

    base = _cfg().get("library_base", "")
    merged = {}
    for rel in paths:
        cache_file = _cache_path("lib", rel.replace("/", "_"))
        text = None
        if _fresh(cache_file):
            with open(cache_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            url = base.rstrip("/") + "/" + urllib.parse.quote(rel)
            text = await _download(session, url, cache_file, timeout)
        if not text:
            continue
        for name, urls in parse_library(text).items():
            merged.setdefault(name, [])
            for u in urls:
                if u not in merged[name]:
                    merged[name].append(u)
    logger.info(f"[Quake组播] {province}-{isp} 地址库解析 {len(merged)} 个频道")
    return merged


# ── Quake 检索 ───────────────────────────────────────────────────────
async def quake_search(session, province, isp, pages=2, timeout=None):
    """按省份+运营商检索 udpxy 节点，返回 [(ip, port), ...]"""
    cfg = _cfg()
    api = cfg.get("api") or "https://quake.360.net/api/v3/search/quake_service"
    headers = {"X-QuakeToken": cfg.get("token", ""), "Content-Type": "application/json"}
    query = f'udpxy AND country_cn: "中国"'
    if province:
        query += f' AND province_cn: "{province}"'
    if isp:
        query += f' AND isp: "{isp}"'

    found, seen = [], set()
    for page in range(max(1, int(pages))):
        body = {
            "query": query,
            "start": page,
            "size": 10,
            "ignore_cache": False,
            "latest": True,
            "include": ["ip", "port", "location.city_cn"],
        }
        # 快捷检索 ID 是账号维度的，别人分享的 ID 在自己账号下会报「快捷检索不存在」
        shortcut = (cfg.get("shortcut") or "").strip()
        if shortcut:
            body["shortcuts"] = [shortcut]
        data = None
        for attempt in range(3):
            try:
                async with session.post(api, json=body, headers=headers,
                                        timeout=timeout or _timeout()) as resp:
                    if resp.status in (401, 403):
                        logger.warning(f"[Quake组播] token 无效或无权限（HTTP {resp.status}），"
                                       f"请检查 config.mcast_quake.token")
                        return found
                    if resp.status != 200:
                        logger.warning(f"[Quake组播] Quake 返回 HTTP {resp.status}")
                        return found
                    data = await resp.json(content_type=None)
            except Exception as e:
                logger.warning(f"[Quake组播] Quake 请求异常({province}{isp}): {e}")
                return found

            if data.get("code") == 0:
                break

            msg = str(data.get("message") or data.get("code"))
            # 基础会员有频率限制，退避重试
            if "频繁" in msg and attempt < 2:
                wait = 4 * (attempt + 1)
                logger.info(f"[Quake组播] 触发频率限制，{wait}s 后重试（{attempt + 1}/2）")
                await asyncio.sleep(wait)
                continue
            logger.warning(f"[Quake组播] Quake 返回异常({province}{isp}): {msg}")
            return found

        if not data or data.get("code") != 0:
            return found

        items = data.get("data") or []
        for it in items:
            ip, port = it.get("ip"), it.get("port")
            if ip and port and (ip, port) not in seen:
                seen.add((ip, port))
                found.append((str(ip), str(port)))
        total = ((data.get("meta") or {}).get("pagination") or {}).get("total") or 0
        if len(found) >= total or not items:
            break
    logger.info(f"[Quake组播] {province}{isp} 检索到 {len(found)} 个 udpxy")
    return found


async def probe_udpxy(session, ip, port, timeout=None):
    """GET /status 判断 udpxy 是否存活"""
    url = f"http://{ip}:{port}/status"
    try:
        async with session.get(url, timeout=timeout or aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status != 200:
                return False
            text = (await resp.text(errors="ignore")).lower()
            return "udpxy" in text or "multicast" in text or "<table" in text
    except Exception:
        return False


# ── 主流程 ───────────────────────────────────────────────────────────
def _paths_from_reference(reference):
    """从现有 channels（或快照）里提取 {地区: {频道: [/rtp/...]}}"""
    regions = {}
    for cat, chans in (reference or {}).items():
        if not cat.startswith("组播源"):
            continue
        label = cat.replace("组播源-", "", 1) if cat.startswith("组播源-") else ""
        for name, urls in chans.items():
            for u in urls:
                m = _RTP_RE.search(u or "")
                if not m:
                    continue
                addr = m.group("addr")
                kind = "/udp/" if "udp" in m.group("kind").lower() else "/rtp/"
                regions.setdefault(label, {}).setdefault(name, [])
                path = kind + addr
                if path not in regions[label][name]:
                    regions[label][name].append(path)
    return regions


def save_snapshot(channels):
    """保存本次成功结果，供主接口挂掉时复用 /rtp/ 路径"""
    paths = _paths_from_reference(channels)
    if not paths:
        return
    try:
        with open(_cache_path("last_mcast.json"), "w", encoding="utf-8") as f:
            json.dump(paths, f, ensure_ascii=False)
        logger.info(f"[Quake组播] 已保存组播快照 {len(paths)} 个地区")
    except Exception as e:
        logger.warning(f"[Quake组播] 保存快照失败: {e}")


def load_snapshot():
    path = _cache_path("last_mcast.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


async def fetch_quake_multicast(reference=None):
    """
    Quake 备用链路主入口，返回与 fetch_mcast.fetch_multicast() 相同的结构：
        {category: {channel_name: [urls]}}

    数据来源优先级：
        1) reference（主接口本次结果）里的 /rtp/ 路径
        2) 上次成功结果的快照 cache/quake/last_mcast.json
        3) 在线组播地址库（按 config.mcast_quake.regions，或快照里的地区）
    """
    if not _enabled():
        logger.info("[Quake组播] 未启用或未配置 token，跳过")
        return {}

    cfg = _cfg()
    max_udpxy = max(1, int(cfg.get("max_udpxy_per_region", 3) or 3))
    max_probe = max(max_udpxy, int(cfg.get("max_udpxy_probe", 10) or 10))
    max_chan = max(1, int(cfg.get("max_channels_per_region", 400) or 400))
    max_urls = max(1, int(cfg.get("max_urls_per_channel", 4) or 4))
    pages = int(cfg.get("pages", 2) or 2)

    ref_paths = _paths_from_reference(reference)
    source = "主接口结果"
    if not ref_paths:
        ref_paths = load_snapshot()
        source = "本地快照"

    wanted = [r for r in (cfg.get("regions") or []) if r]
    plan = {}  # {label: {频道: [路径]}}，值为空 dict 表示该地区要去地址库取
    for r in wanted:
        province, isp = parse_region(r)
        if not province:
            continue
        matched = None
        for label in ref_paths:
            lp, li = parse_region(label)
            if lp.startswith(province) and (not isp or li == isp):
                matched = label
                break
        plan[matched or region_label(province, isp)] = ref_paths.get(matched, {})

    if not plan:
        if ref_paths:
            plan = dict(ref_paths)
        else:
            source = "在线地址库"

    timeout = aiohttp.ClientTimeout(total=_timeout())
    connector = aiohttp.TCPConnector(limit=12, ssl=False)
    channels = {}

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        if not plan:
            index = await load_library_index(session, timeout)
            for province, isp in list(index.keys())[: int(cfg.get("max_regions", 6) or 6)]:
                plan[region_label(province, isp)] = {}

        logger.info(f"[Quake组播] 启动，地区来源: {source}，地区数: {len(plan)}")

        for label, chans in plan.items():
            province, isp = parse_region(label)
            if not province:
                logger.info(f"[Quake组播] 无法识别地区 {label}，跳过")
                continue

            if not chans:
                chans = await load_region_channels(session, province, isp, timeout)
            if not chans:
                continue

            nodes = await quake_search(session, province, isp, pages=pages)
            if not nodes:
                continue

            alive = []
            for ip, port in nodes[:max_probe]:
                if await probe_udpxy(session, ip, port):
                    alive.append((ip, port))
                if len(alive) >= max_udpxy:
                    break
            if not alive:
                logger.info(f"[Quake组播] {province}{isp} 没有可用 udpxy")
                continue

            cat = label if label.startswith("组播源") else region_label(province, isp)
            bucket = channels.setdefault(cat, {})
            items = list(chans.items())[:max_chan]
            for name, paths in items:
                for ip, port in alive:
                    if len(bucket.get(name) or []) >= max_urls:
                        break
                    for p in paths:
                        if len(bucket.setdefault(name, [])) >= max_urls:
                            break
                        url = f"http://{ip}:{port}{p}"
                        if url not in bucket[name]:
                            bucket[name].append(url)
            logger.info(f"[Quake组播] {province}{isp}: {len(alive)} 个 udpxy × "
                        f"{len(items)} 个频道")

    total = sum(len(u) for ch in channels.values() for u in ch.values())
    logger.info(f"[Quake组播] 完成，{len(channels)} 个分类、{total} 个频道-URL 组合")
    return channels
