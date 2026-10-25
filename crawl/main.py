import re
import asyncio
import requests
import logging
from collections import OrderedDict
from datetime import datetime
import config
import check as quality_checker
import fetch_mcast

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", handlers=[logging.FileHandler("function.log", "w", encoding="utf-8"), logging.StreamHandler()])


def parse_template(template_file):
    template_channels = OrderedDict()
    current_category = None

    with open(template_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if "#genre#" in line:
                    current_category = line.split(",")[0].strip()
                    template_channels[current_category] = []
                elif current_category:
                    channel_name = line.split(",")[0].strip()
                    template_channels[current_category].append(channel_name)

    return template_channels


def parse_alias_template(template_file):
    """解析频道别名模板：每行为「分组名,#genre#」或「规范频道名：别名1#别名2#...」。
    返回 OrderedDict{分组: [(规范名, [别名...]), ...]}，保持模板顺序。"""
    template = OrderedDict()
    current_group = None
    with open(template_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("#EXT"):
                continue
            if "#genre#" in line:
                current_group = line.split(",")[0].strip()
                template.setdefault(current_group, [])
                continue
            if current_group is None:
                continue
            if "：" in line:
                name, aliases = line.split("：", 1)
            elif ":" in line:
                name, aliases = line.split(":", 1)
            else:
                continue
            name = name.strip()
            alias_list = [a.strip() for a in aliases.split("#") if a.strip()]
            if name:
                template[current_group].append((name, alias_list))
    logging.info(f"[模板] 解析别名模板成功，共 {len(template)} 个分组")
    return template


def remap_by_template(channels, template_map):
    """将 channels {cat: {name: [urls]}} 按模板别名归一到「模板分组 + 规范频道名」。
    返回 (matched, leftover)：
      matched : OrderedDict{模板分组: OrderedDict{规范名: [urls]}}，按模板顺序
      leftover: OrderedDict{原分组: OrderedDict{原频道名: [urls]}}，模板未覆盖的频道
    同一别名在模板多处出现时按「先到先得」，只归入第一个分组。"""
    lookup = {}
    for group, entries in template_map.items():
        for name, aliases in entries:
            for cand in [name] + aliases:
                key = _normalize(cand)
                if key and key not in lookup:
                    lookup[key] = (group, name)

    matched = OrderedDict()
    leftover_all = OrderedDict()
    matched_cnt = 0
    for cat, ch_dict in channels.items():
        for ch_name, urls in ch_dict.items():
            hit = lookup.get(_normalize(ch_name))
            if hit:
                group, std_name = hit
                matched.setdefault(group, OrderedDict()).setdefault(std_name, []).extend(urls)
                matched_cnt += len(urls)
            else:
                # 模板未覆盖的频道统一归入「其他频道」，避免按来源散落大量分组
                leftover_all.setdefault(ch_name, []).extend(urls)
    leftover = OrderedDict()
    if leftover_all:
        leftover["其他频道"] = leftover_all
    logging.info(f"[模板] 归组完成：模板命中 {matched_cnt} 个 URL，未命中频道并入「其他频道」")
    return matched, leftover


def fetch_epg_id_map():
    """从 config.epg_urls 依次加载 EPG，按频道名合并映射。保底源放末尾。"""
    import gzip
    import xml.etree.ElementTree as ET
    epg_id_map = {}
    for epg_url in config.epg_urls:
        try:
            resp = requests.get(epg_url, timeout=15)
            resp.raise_for_status()
            # .gz 链接需要解压，.xml 链接直接用
            if epg_url.endswith(".gz"):
                xml_data = gzip.decompress(resp.content)
            else:
                xml_data = resp.content
            tree = ET.fromstring(xml_data)
            for ch in tree.findall("channel"):
                dn = ch.find("display-name")
                if dn is not None and dn.text:
                    eid = ch.get("id", "").strip()
                    name = dn.text.strip()
                    if eid and name:
                        epg_id_map.setdefault(name, eid)
            logging.info(f"[EPG映射] 从 {epg_url} 加载成功")
        except Exception as e:
            logging.warning(f"[EPG映射] 从 {epg_url} 加载失败，尝试下一个源: {e}")
    if epg_id_map:
        logging.info(f"[EPG映射] 共合并加载 {len(epg_id_map)} 个频道 ID")
    else:
        logging.warning("[EPG映射] 所有 EPG 源均加载失败，将使用默认数字 ID")
    return epg_id_map




def fetch_channels(url):
    channels = OrderedDict()

    try:
        response = requests.get(url)
        response.raise_for_status()
        response.encoding = "utf-8"
        lines = response.text.split("\n")
        current_category = None
        is_m3u = any("#EXTINF" in line for line in lines[:15])
        source_type = "m3u" if is_m3u else "txt"
        logging.info(f"url: {url} 获取成功，判断为{source_type}格式")

        if is_m3u:
            for line in lines:
                line = line.strip()
                if line.startswith("#EXTINF"):
                    match = re.search(r'group-title="(.*?)"', line)
                    if match:
                        current_category = match.group(1).strip()
                        name_match = re.search(r',([^,]*)$', line)
                        channel_name = name_match.group(1).strip() if name_match else ""
                        if current_category not in channels:
                            channels[current_category] = []
                elif line and not line.startswith("#"):
                    channel_url = line.strip()
                    if current_category and channel_name:
                        channels[current_category].append((channel_name, channel_url))
        else:
            for line in lines:
                line = line.strip()
                if "#genre#" in line:
                    current_category = line.split(",")[0].strip()
                    channels[current_category] = []
                elif current_category:
                    match = re.match(r"^(.*?),(.*?)$", line)
                    if match:
                        channel_name = match.group(1).strip()
                        channel_url = match.group(2).strip()
                        channels[current_category].append((channel_name, channel_url))
                    elif line:
                        channels[current_category].append((line, ""))
        if channels:
            categories = ", ".join(channels.keys())
            logging.info(f"url: {url} 抓取成功，包含频道分类: {categories}")
    except requests.RequestException as e:
        logging.error(f"url: {url} 抓取失败。 Error: {e}")

    return channels


def _normalize(name: str) -> str:
    s = name.strip()
    s = re.sub(r'[（\[(（\[).?[）\]\)]', '', s)
    s = re.sub(r'(高清版|超清版|频道|卫视|高清|超清|HD|台)$', '', s)
    while re.search(r'(高清版|超清版|频道|卫视|高清|超清|HD|台)$', s):
        s = re.sub(r'(高清版|超清版|频道|卫视|高清|超清|HD|台)$', '', s)
    s = s.replace('-', '').replace(' ', '')
    return s


def match_channels(template_channels, all_channels):
    matched_channels = OrderedDict()

    for category, channel_list in template_channels.items():
        matched_channels[category] = OrderedDict()
        for channel_name in channel_list:
            norm_target = _normalize(channel_name)
            for online_category, online_channel_list in all_channels.items():
                for online_channel_name, online_channel_url in online_channel_list:
                    if _normalize(online_channel_name) == norm_target:
                        matched_channels[category].setdefault(channel_name, []).append(online_channel_url)

    return matched_channels


def filter_source_urls(template_file):
    template_channels = parse_template(template_file)
    source_urls = config.source_urls

    all_channels = OrderedDict()
    for url in source_urls:
        fetched_channels = fetch_channels(url)
        for category, channel_list in fetched_channels.items():
            if category in all_channels:
                all_channels[category].extend(channel_list)
            else:
                all_channels[category] = channel_list

    # 将 {cat: [(name, url), ...]} 转为 {cat: {name: [url, ...]}}
    grouped = OrderedDict()
    for category, channel_list in all_channels.items():
        grouped[category] = OrderedDict()
        for channel_name, channel_url in channel_list:
            if channel_url:
                grouped[category].setdefault(channel_name, []).append(channel_url)

    return grouped, template_channels


def is_ipv6(url):
    """判断是否为 IPv6 地址"""
    clean_url = url.rstrip("$")
    return re.match(r"^https?://\[.+\]", clean_url) is not None


def _get_source_type(url: str, category: str = "") -> str:
    """识别 URL 来源类型：mcast（组播源）或 subscription（订阅源）
    组播源的 category 以「组播源」开头，与订阅源分类名不冲突。"""
    if category.startswith("组播源"):
        return "mcast"
    return "subscription"




def _url_sort_key(url: str, check_results: dict, category: str = ""):
    """
    排序 key：IP版本优先 > 来源（根据 source_priority） > 质量
    ip_version_priority="ipv6" 时 IPv6 优先，否则 IPv4 优先
    source_priority="mcast" 时组播源优先，否则订阅源优先
    返回 (ipv6_rank, source_rank, layer_rank, -bitrate, -width)，sorted 升序排列
    """
    clean = url.split(chr(36), 1)[0] if chr(36) in url else url
    # IP 版本排序：优先选择 config 中指定的版本
    is_v6 = is_ipv6(url)
    ipv6_first = config.ip_version_priority == "ipv6"
    ipv6_rank = 0 if (is_v6 and ipv6_first) or (not is_v6 and not ipv6_first) else 1
    # 来源排序：根据 source_priority 配置
    source_priority = config.source_priority
    source_rank = 0 if _get_source_type(url, category) == source_priority else 1
    # 质量排序
    layer = "fast"
    bitrate = 0
    width = 0
    speed_kbps = 0  # 新增：下载速度
    if check_results:
        for cat_ch in check_results.values():
            for ch_urls in cat_ch.values():
                r = ch_urls.get(clean, {})
                if r:
                    layer = r.get("layer", "fast")
                    fp = r.get("ffprobe", {})
                    if fp:
                        bitrate = fp.get("bitrate", 0)
                        width = fp.get("width", 0)
                    # 新增：获取速度信息
                    deep = r.get("deep", {})
                    if deep:
                        speed_kbps = deep.get("speed_kbps", 0)
                    break
    layer_rank = 0 if layer in ("ffprobe", "deep") else 1
    # 排序：IP 版本 > 来源 > 层级 > 速度 > 码率 > 分辨率
    return (ipv6_rank, source_rank, layer_rank, -speed_kbps, -bitrate, -width)

def _get_meta_suffix(url: str, check_results: dict) -> str:
    """从 check_results 提取 ffprobe 元数据和速度信息，生成后缀如 【1920x1080@256kbps 1.7Mbps】"""
    clean = url.split(chr(36), 1)[0] if chr(36) in url else url
    if not check_results:
        return ""
    for cat_ch in check_results.values():
        for ch_urls in cat_ch.values():
            r = ch_urls.get(clean, {})
            fp = r.get("ffprobe", {})
            # 获取深度探测的速度信息
            deep = r.get("deep", {})
            speed_kbps = deep.get("speed_kbps", 0) if deep else 0
            
            if fp and fp.get("status") == "ok":
                w, h = fp.get("width", 0), fp.get("height", 0)
                br = fp.get("bitrate", 0)
                parts = []
                if w and h:
                    parts.append(f"{w}x{h}")
                if br > 0:
                    parts.append(f"{br//1000}kbps")
                # 添加速度信息
                if speed_kbps > 0:
                    if speed_kbps >= 1000:
                        parts.append(f"{speed_kbps//1000}Mbps")
                    else:
                        parts.append(f"{speed_kbps}kbps")
                if parts:
                    # 分辨率和码率用 @ 连接，速度单独放后面
                    if len(parts) >= 2 and "x" in parts[0]:
                        # 有分辨率，格式：【1920x1080@256kbps 4Mbps】
                        res_br = "@".join(parts[:-1])
                        speed = parts[-1]
                        return f" 【{res_br} {speed}】"
                    else:
                        return " 【" + " ".join(parts) + "】"
            elif speed_kbps > 0:
                # 组播/rtp 流：ffprobe 读不到元数据，仅附加测速信息
                if speed_kbps >= 1000:
                    return f" 【{speed_kbps//1000}Mbps】"
                return f" 【{speed_kbps}kbps】"
    return ""

def _print_domain_suggestions(fail_domains: dict):
    """打印检测失败的域名建议列表，供用户考虑加入黑名单"""
    if not fail_domains:
        return
    summary = {}
    for domain, entries in sorted(fail_domains.items(), key=lambda x: -len(x[1])):
        statuses = {}
        for e in entries:
            s = e["status"]
            statuses[s] = statuses.get(s, 0) + 1
        status_str = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
        summary[domain] = f"失败次数={len(entries)}  ({status_str})"

    logging.info("[黑名单建议] 以下域名检测频繁失败，可考虑加入 url_blacklist：")
    for domain, info in summary.items():
        logging.info(f"  {domain}  {info}")


async def async_main():
    """异步主入口：fetch -> check -> remap -> write"""
    epg_id_map = fetch_epg_id_map()
    template_file = "template.txt"
    template_map = parse_alias_template(template_file)
    channels, _ = filter_source_urls(template_file)

    # 组播源抓取
    if config.mcast_config.get("enabled", False):
        logging.info("[组播源] 开始抓取...")
        mcast_channels = await fetch_mcast.fetch_multicast()
        # 组播源数据是 {cat: {name: [urls]}}，直接合并到 channels
        merged = 0
        for cat, ch_dict in mcast_channels.items():
            for name, urls in ch_dict.items():
                channels.setdefault(cat, {}).setdefault(name, []).extend(urls)
                merged += len(urls)
        logging.info(f"[组播源] 合并完成，共添加 {merged} 个 URL")

    check_results = {}
    if config.enable_quality_check:
        logging.info("[质量检测] 开始...")
        check_results, fail_domains = await quality_checker.check_all(channels)
        channels = quality_checker.filter_dead_urls(channels, check_results)
        _print_domain_suggestions(fail_domains)
        logging.info("[质量检测] 完成")

    matched, leftover = remap_by_template(channels, template_map)
    updateChannelUrlsM3U(matched, leftover, epg_id_map, check_results)


def updateChannelUrlsM3U(matched, leftover, epg_id_map=None, check_results=None):
    written_urls = set()
    epg_id_map = epg_id_map or {}

    current_date = datetime.now().strftime("%Y-%m-%d")
    for group in config.announcements:
        for announcement in group["entries"]:
            name = announcement.get("name")
            if name is None or name == "__TIME__":
                name = current_date
            elif isinstance(name, str) and "__TIME__" in name:
                name = name.replace("__TIME__", current_date)
            announcement["name"] = name

    def write_group(category, channel_dict):
        f_txt.write(f"{category},#genre#\n")
        for channel_name, url_list in channel_dict.items():
            sorted_urls = sorted(
                url_list,
                key=lambda url: _url_sort_key(url, check_results, category)
            )
            filtered_urls = []
            for url in sorted_urls:
                if url and url not in written_urls and not any(blacklist in url for blacklist in config.url_blacklist):
                    filtered_urls.append(url)
                    written_urls.add(url)

            # 限制每频道最大线路数
            if config.max_lines_per_channel > 0 and len(filtered_urls) > config.max_lines_per_channel:
                old_count = len(filtered_urls)
                filtered_urls = filtered_urls[:config.max_lines_per_channel]
                logging.info("[频道] %s 线路从 %d 截断至 %d", channel_name, old_count, config.max_lines_per_channel)
            total_urls = len(filtered_urls)
            for index, url in enumerate(filtered_urls, start=1):
                if "$" in url:
                    base_url = url.split("$", 1)[0]
                else:
                    base_url = url

                # 默认输出纯地址：$LR—IPV4【线路N】【分辨率@码率】 这类后缀部分播放器不认，
                # 还会让订阅地址看起来很长。需要标注时把 config.url_suffix_enabled 设为 True。
                if getattr(config, "url_suffix_enabled", False):
                    extra = _get_meta_suffix(url, check_results)
                    if is_ipv6(url):
                        url_suffix = f"$LR—IPV6{extra}" if total_urls == 1 else f"$LR—IPV6【线路{index}】{extra}"
                    else:
                        url_suffix = f"$LR—IPV4{extra}" if total_urls == 1 else f"$LR—IPV4【线路{index}】{extra}"
                    new_url = f"{base_url}{url_suffix}"
                else:
                    new_url = base_url

                tvg_id = epg_id_map.get(channel_name, channel_name)
                f_m3u.write(f"#EXTINF:-1 tvg-id=\"{tvg_id}\" tvg-name=\"{channel_name}\" tvg-logo=\"https://gcore.jsdelivr.net/gh/yuanzl77/TVlogo@master/png/{channel_name}.png\" group-title=\"{category}\",{channel_name}\n")
                f_m3u.write(new_url + "\n")
                f_txt.write(f"{channel_name},{new_url}\n")

    with open("live.m3u", "w", encoding="utf-8") as f_m3u:
        epg_attr = ",".join(chr(34)+epg_url+chr(34) for epg_url in config.epg_urls)
        f_m3u.write(f"#EXTM3U x-tvg-url={epg_attr}\n")
        f_m3u.write(f"# 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）\n")

        with open("live.txt", "w", encoding="utf-8") as f_txt:
            f_txt.write(f"# 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）\n")
            # 更新时间分组：播放器里会显示成一个分组，点开能看到本次生成时间
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 取一条真实线路作为"更新时间"条目的地址，避免播放器把死链连分组一起隐藏
            sample_url = ""
            for _ch_dict in matched.values():
                for _urls in _ch_dict.values():
                    if _urls:
                        sample_url = _urls[0]
                        break
                if sample_url:
                    break
            sample_url = sample_url or getattr(config, "self_url", "")
            f_txt.write(f"更新时间 {stamp},#genre#\n")
            f_txt.write(f"{stamp} 更新,{sample_url}\n")
            f_m3u.write(
                f'#EXTINF:-1 tvg-id="更新" tvg-name="更新" group-title="更新时间 {stamp}",{stamp} 更新\n'
            )
            f_m3u.write(f"{sample_url}\n")

            for group in config.announcements:
                f_txt.write(f"{group['channel']},#genre#\n")
                for announcement in group["entries"]:
                    f_m3u.write(f"""#EXTINF:-1 tvg-id="{announcement['name']}" tvg-name="{announcement['name']}" tvg-logo="{announcement['logo']}" group-title="{group['channel']}",{announcement['name']}\n""")
                    f_m3u.write(f"{announcement['url']}\n")
                    f_txt.write(f"{announcement['name']},{announcement['url']}\n")

            # 1) 按模板分组输出（模板命中频道，按模板分组/频道顺序）
            for category, channel_dict in matched.items():
                write_group(category, channel_dict)

            # 2) 模板未覆盖的频道，按原来源分组追加
            for category, channel_dict in leftover.items():
                write_group(category, channel_dict)

            f_txt.write("\n")


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    finally:
        quality_checker._shutdown_ffprobe_executor()
