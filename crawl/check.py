"""
IPTV 质量检测模块
双引擎：HTTP 快筛 + FFprobe 中度探测
"""
import re
import asyncio
import json
import time
import logging
import subprocess
import aiohttp
import config

_ffprobe_executor = None

logger = logging.getLogger(__name__)


def _strip_suffix(url: str) -> str:
    """去掉 $LR... 等播放器自定义后缀"""
    if "$" in url:
        return url.split("$", 1)[0]
    return url


def _get_domain(url: str) -> str:
    """从 URL 中提取基础域名（不含路径和端口）"""
    if not url:
        return ""
    stripped = url.split("$", 1)[0] if "$" in url else url
    m = re.match(r"(https?://(?:\[?[^\[/\]]+\]?)?)", stripped)
    return m.group(1) if m else ""


def _is_m3u8_url(url: str) -> bool:
    """判断是否为 m3u8 地址"""
    return ".m3u8" in _strip_suffix(url) or "index.m3u8" in _strip_suffix(url)


def _get_base_url(url: str) -> str:
    """获取 URL 的基地址（用于拼接相对路径的 TS 片段）"""
    stripped = _strip_suffix(url)
    idx = stripped.rfind("/")
    return stripped[:idx + 1] if idx != -1 else stripped


async def _http_fast_check(session, url, timeout):
    """HTTP 快筛：拉 playlist / 组播流验证可达性"""
    result = {"url": url, "status": "unknown", "detail": "", "layer": "fast"}
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                result["status"] = "failed"
                result["detail"] = f"http_status={resp.status}"
                return result
            if ".m3u8" in url or "index.m3u8" in url:
                text = await resp.text()
                if not text or len(text) < 10:
                    result["status"] = "empty"
                    result["detail"] = "playlist is empty"
                    return result
                ts_lines = [l.strip() for l in text.splitlines()
                            if l.strip() and not l.strip().startswith("#") and l.strip()]
                result["ts_count"] = len(ts_lines)
                if not ts_lines:
                    result["status"] = "ok_no_ts"
                    result["detail"] = "live m3u8 (no ENDLIST)"
                else:
                    result["status"] = "ok"
                    result["detail"] = f"playlist_ok ts_entries={len(ts_lines)}"
            else:
                # 非 m3u8（组播/rtp 隧道等持续流）：读取少量字节验证可播后测速
                try:
                    chunk = await resp.content.readexactly(1)
                    result["status"] = "ok"
                    # 组播源测速：读满 min_bytes 或达到窗口时间即停止
                    total = len(chunk)
                    start = time.monotonic()
                    try:
                        while True:
                            elapsed = time.monotonic() - start
                            if elapsed >= config.speed_probe_seconds or total >= config.speed_probe_min_bytes:
                                break
                            chunk = await asyncio.wait_for(
                                resp.content.read(65536),
                                timeout=config.speed_probe_seconds - elapsed,
                            )
                            if not chunk:
                                break
                            total += len(chunk)
                    except asyncio.TimeoutError:
                        pass
                    elapsed = time.monotonic() - start
                    if elapsed > 0.05:
                        speed_kbps = int((total * 8) / elapsed / 1000)
                    else:
                        speed_kbps = 0
                    result["deep"] = {
                        "status": "ok",
                        "speed_kbps": speed_kbps,
                        "detail": f"speed_probe {speed_kbps}kbps",
                    }
                    result["detail"] = f"http_ok stream {speed_kbps}kbps"
                except asyncio.IncompleteReadError:
                    result["status"] = "empty"
                    result["detail"] = "stream closed early"
    except asyncio.TimeoutError:
        result["status"] = "timeout"
        result["detail"] = f"timeout >{timeout}s"
    except Exception as e:
        result["status"] = "error"
        result["detail"] = str(e)
    return result


def _find_ffprobe():
    """查找 ffprobe 可执行文件"""
    import shutil
    if config.ffmpeg_path:
        return config.ffmpeg_path
    path = shutil.which("ffprobe")
    return path or "ffprobe"


def _run_ffprobe(url, timeout, max_streams):
    """运行 ffprobe 探流，返回元数据字典"""
    ffprobe = _find_ffprobe()
    cmd = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-i", url,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=False,
            timeout=timeout + 2,
        )
        if proc.returncode != 0:
            return {"status": "failed", "detail": f"ffprobe exit={proc.returncode}"}
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        result = {
            "status": "ok",
            "detail": "",
            "video_codec": video_stream.get("codec_name", "") if video_stream else "",
            "width": video_stream.get("width", 0) if video_stream else 0,
            "height": video_stream.get("height", 0) if video_stream else 0,
            "bitrate": 0,
        }
        if video_stream and video_stream.get("bit_rate"):
            result["bitrate"] = int(video_stream["bit_rate"])
        elif audio_stream and audio_stream.get("bit_rate"):
            result["bitrate"] = int(audio_stream["bit_rate"])
        detail_parts = []
        if video_stream:
            res = f"{result['width']}x{result['height']}"
            detail_parts.append(f"v:{video_stream['codec_name']}@{res}")
        if audio_stream:
            detail_parts.append(f"a:{audio_stream['codec_name']}")
        if result["bitrate"] > 0:
            detail_parts.append(f"br:{result['bitrate']//1000}kbps")
        result["detail"] = " ".join(detail_parts)
        return result
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": f"ffprobe timeout >{timeout}s"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}




def _deep_probe_m3u8(url, timeout):
    """深度探测 m3u8 直播流：检查分片稳定性、播放连续性和下载速度"""
    ffprobe = _find_ffprobe()
    result = {
        "status": "ok",
        "detail": "",
        "target_duration": 0,
        "segment_count": 0,
        "is_live": False,
        "quality_score": 0,
        "speed_kbps": 0,  # 新增：下载速度（kbps）
        "bandwidth_score": 0,  # 新增：带宽评分
    }
    
    # ===== 第一部分：ffprobe 基础探测 =====
    cmd = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_entries", "format=format_name,duration",
        "-i", url,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=False,
            timeout=timeout + 2,
        )
        if proc.returncode != 0:
            result["status"] = "failed"
            result["detail"] = f"ffprobe exit={proc.returncode}"
            return result
        
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        fmt = data.get("format", {})
        format_name = fmt.get("format_name", "")
        if "hls" not in format_name and "mpegts" not in format_name:
            result["status"] = "skip"
            result["detail"] = "not_hls_stream"
            return result
        
        cmd2 = [
            ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-show_entries", "format=target_duration,duration",
            "-i", url,
        ]
        proc2 = subprocess.run(
            cmd2, capture_output=True, text=False,
            timeout=timeout,
        )
        if proc2.returncode == 0:
            data2 = json.loads(proc2.stdout.decode("utf-8", errors="replace"))
            target_dur = data2.get("format", {}).get("target_duration", 0)
            duration = data2.get("format", {}).get("duration", 0)
            result["target_duration"] = int(target_dur) if target_dur else 0
            result["duration"] = float(duration) if duration else 0
            result["is_live"] = True
            if target_dur > 0 and duration > 0:
                result["segment_count"] = int(duration / target_dur)
        
        if result["target_duration"] > 0:
            if 2 <= result["target_duration"] <= 10:
                result["quality_score"] = 80
            elif result["target_duration"] < 2:
                result["quality_score"] = 60
            else:
                result["quality_score"] = 50
        
        if result["segment_count"] > 0:
            result["detail"] = f"hls segments~{result['segment_count']} target_dur={result['target_duration']}s"
        else:
            result["detail"] = "hls_stream detected"
            
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["detail"] = f"deep_probe timeout >{timeout}s"
    except Exception as e:
        result["status"] = "error"
        result["detail"] = str(e)
    
    # ===== 第二部分：速度测试（下载多个 TS 分片） =====
    if result["status"] in ("ok", "ok_no_ts") and _is_m3u8_url(url):
        import time
        import urllib.request
        
        # 下载 m3u8 playlist
        m3u8_start = time.time()
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                m3u8_content = resp.read().decode("utf-8", errors="replace")
            m3u8_elapsed = time.time() - m3u8_start
        except:
            result["detail"] += " m3u8_fail"
            result["speed_kbps"] = 0
            return result
        
        # 解析 m3u8，收集 TS 分片 URL（最多 5 个）
        base_url = _get_base_url(url)
        ts_urls = []
        for line in m3u8_content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if line.endswith(".ts"):
                    ts_urls.append(line)
                elif "/" not in line and base_url and len(ts_urls) < 5:
                    ts_urls.append(base_url + line)
            if len(ts_urls) >= 5:
                break
        
        if not ts_urls:
            result["detail"] += " no_ts"
            result["speed_kbps"] = 0
            return result
        
        # 下载前 3 个 TS 分片，综合计算速度
        total_bytes = len(m3u8_content.encode())
        total_time = m3u8_elapsed
        ts_downloaded = 0
        
        for ts_rel in ts_urls[:3]:
            # 处理相对路径和绝对路径
            ts_url = ts_rel if ts_rel.startswith("http") else base_url + ts_rel
            
            try:
                ts_start = time.time()
                with urllib.request.urlopen(ts_url, timeout=3) as ts_resp:
                    ts_data = ts_resp.read()
                ts_elapsed = time.time() - ts_start
                
                total_bytes += len(ts_data)
                total_time += ts_elapsed
                ts_downloaded += 1
            except:
                continue
        
        # 计算综合速度
        if total_time > 0:
            speed_kbps = total_bytes * 8 / total_time / 1024
            result["speed_kbps"] = int(speed_kbps)
            result["detail"] += f" {ts_downloaded}ts_avg"
        else:
            result["speed_kbps"] = 0
        
        # 根据速度评分
        speed = result["speed_kbps"]
        if speed >= 3000:  # >= 3 Mbps
            result["bandwidth_score"] = 90
        elif speed >= 2000:  # >= 2 Mbps
            result["bandwidth_score"] = 70
        elif speed >= 1000:  # >= 1 Mbps
            result["bandwidth_score"] = 50
        else:
            result["bandwidth_score"] = 30
        
        # 更新 detail 信息
        if result["speed_kbps"] > 0:
            result["detail"] += f" speed={result['speed_kbps']}kbps"
        else:
            result["detail"] += " speed=unknown"
    
    return result


def _playback_test(url, timeout, test_duration=3):
    """播放测试：尝试解码前 N 秒内容，验证可播放性"""
    ffprobe = _find_ffprobe()
    result = {
        "status": "ok",
        "detail": "",
        "decoded_frames": 0,
        "decode_errors": 0,
    }
    
    cmd = [
        "ffmpeg",
        "-y",
        "-v", "error",
        "-i", url,
        "-t", str(test_duration),
        "-f", "null",
        "-"
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout + 5,
        )
        
        stderr = proc.stderr or ""
        
        import re as re_module
        frames_match = re_module.search(r"frame=\s*(\d+)", stderr)
        if frames_match:
            result["decoded_frames"] = int(frames_match.group(1))
        
        error_match = re_module.findall(r"error|Error", stderr)
        result["decode_errors"] = len(error_match)
        
        if proc.returncode != 0 and result["decoded_frames"] == 0:
            result["status"] = "failed"
            result["detail"] = "decode_failed"
        elif result["decode_errors"] > 10:
            result["status"] = "degraded"
            result["detail"] = f"high_error_rate errors={result['decode_errors']}"
        else:
            result["status"] = "ok"
            result["detail"] = f"decoded={result['decoded_frames']}f errors={result['decode_errors']}"
            
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["detail"] = f"playback_test timeout >{timeout}s"
    except Exception as e:
        result["status"] = "error"
        result["detail"] = str(e)
    
    return result


async def _get_ffprobe_executor():
    global _ffprobe_executor
    if _ffprobe_executor is None:
        import concurrent.futures
        _ffprobe_executor = concurrent.futures.ProcessPoolExecutor(max_workers=int(getattr(config, "ffprobe_max_workers", 8)))
    return _ffprobe_executor


def _shutdown_ffprobe_executor():
    """全局清理：关闭 ffprobe 进程池，避免 Event loop 关闭时的 RuntimeError"""
    global _ffprobe_executor
    if _ffprobe_executor is not None:
        try:
            _ffprobe_executor.shutdown(wait=False)
        except Exception:
            pass
        _ffprobe_executor = None


async def _ffprobe_async(url, timeout, max_streams):
    """在独立进程池中异步运行 ffprobe"""
    loop = asyncio.get_event_loop()
    executor = await _get_ffprobe_executor()
    return await loop.run_in_executor(executor, _run_ffprobe, url, timeout, max_streams)


async def _deep_probe_async(url, timeout):
    """异步运行深度探测"""
    loop = asyncio.get_event_loop()
    executor = await _get_ffprobe_executor()
    return await loop.run_in_executor(executor, _deep_probe_m3u8, url, timeout)


async def _playback_test_async(url, timeout, test_duration=3):
    """异步运行播放测试"""
    loop = asyncio.get_event_loop()
    executor = await _get_ffprobe_executor()
    return await loop.run_in_executor(executor, _playback_test, url, timeout, test_duration)


async def _check_single(session, url, http_timeout, ffprobe_timeout, ffprobe_semaphore=None):
    """单 URL 三层检测：HTTP 快筛 + FFprobe 基础探测 + 深度探测"""
    clean_url = _strip_suffix(url)
    # 第一层：HTTP 快筛
    fast = await _http_fast_check(session, clean_url, http_timeout)
    if fast["status"] not in ("ok", "ok_no_ts"):
        fast["layer"] = "fast_fail"
        return fast
    # 第二层：FFprobe 基础探测
    if config.enable_ffprobe:
        probe = await _ffprobe_async(clean_url, ffprobe_timeout, config.ffprobe_max_streams)
        # ffprobe 失败不阻塞，保留快筛结果
        if probe["status"] != "ok":
            fast["ffprobe"] = probe
            return fast
        # 质量过滤：bitrate 为 0 说明 ffprobe 无法读取码率字段（常见于 IPTV），跳过码率检查只检查分辨率
        min_br = config.min_bitrate if config.min_bitrate > 0 else 0
        if min_br > 0 and probe.get("bitrate", 0) > 0 and probe.get("bitrate", 0) < min_br:
            probe["status"] = "failed"
            probe["detail"] += f" low_bitrate={probe['bitrate']//1000}kbps < {min_br//1000}kbps"
            probe["layer"] = "ffprobe_fail"
            fast["ffprobe"] = probe
            return fast
        min_res = int(config.min_resolution) if config.min_resolution else 0
        if min_res > 0 and probe.get("width", 0) < min_res:
            probe["status"] = "failed"
            probe["detail"] += f" low_resolution={probe['width']}px < {min_res}px"
            probe["layer"] = "ffprobe_fail"
            fast["ffprobe"] = probe
            return fast
        fast["ffprobe"] = probe
        # 第三层：深度探测（仅对 m3u8 流；组播/rtp 流已在快筛阶段完成测速）
        if config.enable_deep_probe and _is_m3u8_url(clean_url):
            deep = await _deep_probe_async(clean_url, config.deep_probe_timeout)
            if deep["status"] == "ok":
                fast["deep"] = deep
                fast["layer"] = "deep"
            else:
                fast["deep"] = deep
                # 深度探测失败不阻塞，保留基础探测结果
                fast["layer"] = "ffprobe"
        else:
            fast["layer"] = "ffprobe"
    return fast


async def check_all(channels):
    """
    三层质量检测（HTTP 快筛 → ffprobe → 深度探测）。

    优化点：
    - 按唯一 URL 去重，同一个地址只测一次（源站里同一 URL 常挂在多个频道下）；
    - 三层各自独立限流，慢的 ffprobe 不会占住 HTTP 的并发位；
    - ffprobe 失败但 HTTP 已拿到合法播放列表时标记为 fast_ok（B 级），
      由 filter_dead_urls 决定是否保留，避免起播慢的好源被误杀。

    参数:
        channels: {category: {channel_name: [url1, url2, ...]}}

    返回:
        (check_results, fail_domains)，results 仍按 {cat: {name: {url: r}}} 组织。
    """
    unique = {}
    combos = 0
    for cat, ch_dict in channels.items():
        for ch_name, url_list in ch_dict.items():
            for url in url_list:
                combos += 1
                unique.setdefault(url, True)
    urls = list(unique.keys())

    max_http = max(1, int(getattr(config, "check_max_conn", 30)))
    max_ff = max(1, int(getattr(config, "ffprobe_max_workers", 8)))
    max_deep = max(1, int(getattr(config, "deep_probe_max_conn", 16)))
    sem_http = asyncio.Semaphore(max_http)
    sem_ff = asyncio.Semaphore(max_ff)
    sem_deep = asyncio.Semaphore(max_deep)

    logger.info(
        f"开始质量检测：{combos} 个频道-URL 组合，去重后 {len(urls)} 个唯一 URL；"
        f"HTTP 并发 {max_http} / 超时 {config.check_timeout}s"
        + (f"，ffprobe 并发 {max_ff} / 超时 {config.ffprobe_timeout}s" if config.enable_ffprobe else "")
        + (f"，深度探测并发 {max_deep} / 超时 {config.deep_probe_timeout}s" if config.enable_deep_probe else "")
    )

    async def _worker(session, url):
        clean = _strip_suffix(url)
        async with sem_http:
            fast = await _http_fast_check(session, clean, config.check_timeout)
        if fast["status"] not in ("ok", "ok_no_ts"):
            fast["layer"] = "fast_fail"
            return url, fast
        if not config.enable_ffprobe:
            fast["layer"] = "fast_ok"
            return url, fast

        try:
            async with sem_ff:
                probe = await _ffprobe_async(clean, config.ffprobe_timeout, config.ffprobe_max_streams)
        except Exception as e:
            # 进程池异常不能把整条源弄丢：退回 HTTP 级判定
            fast["ffprobe"] = {"status": "error", "detail": "ffprobe executor: %s" % e}
            fast["layer"] = "fast_ok"
            return url, fast
        if probe["status"] != "ok":
            # HTTP 已经验证可达、播放列表合法；ffprobe 失败只降级不丢弃
            fast["ffprobe"] = probe
            fast["layer"] = "fast_ok"
            return url, fast

        min_br = config.min_bitrate if getattr(config, "min_bitrate", 0) > 0 else 0
        if min_br > 0 and probe.get("bitrate", 0) > 0 and probe.get("bitrate", 0) < min_br:
            probe["status"] = "failed"
            probe["detail"] += f" low_bitrate={probe['bitrate']//1000}kbps < {min_br//1000}kbps"
            fast["ffprobe"] = probe
            fast["layer"] = "fast_fail"
            return url, fast

        min_res = int(getattr(config, "min_resolution", 0) or 0)
        if min_res > 0 and probe.get("width", 0) < min_res:
            probe["status"] = "failed"
            probe["detail"] += f" low_resolution={probe['width']}px < {min_res}px"
            fast["ffprobe"] = probe
            fast["layer"] = "fast_fail"
            return url, fast

        fast["ffprobe"] = probe
        if config.enable_deep_probe:
            try:
                async with sem_deep:
                    deep = await _deep_probe_async(clean, config.deep_probe_timeout)
            except Exception as e:
                deep = {"status": "error", "detail": "deep probe: %s" % e}
            fast["deep"] = deep
            fast["layer"] = "deep" if deep["status"] == "ok" else "ffprobe"
        else:
            fast["layer"] = "ffprobe"
        return url, fast

    connector = aiohttp.TCPConnector(limit=max_http, ssl=False)
    timeout = aiohttp.ClientTimeout(total=max(config.check_timeout, config.ffprobe_timeout) + 5)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        pairs = await asyncio.gather(*(_worker(session, u) for u in urls), return_exceptions=True)

    url_result = {}
    errors = 0
    for item in pairs:
        if isinstance(item, Exception):
            errors += 1
            continue
        u, r = item
        url_result[u] = r
    if errors:
        logger.warning(f"检测过程中有 {errors} 个 URL 抛出未捕获异常（已跳过）")

    results = {}
    fail_domains = {}
    for cat, ch_dict in channels.items():
        for ch_name, url_list in ch_dict.items():
            for url in url_list:
                r = url_result.get(url)
                if r is None:
                    continue
                results.setdefault(cat, {}).setdefault(ch_name, {})[url] = r
                if r.get("status") in ("ok", "ok_no_ts") and r.get("layer") in ("ffprobe", "deep", "fast_ok"):
                    continue
                domain = _get_domain(url)
                if domain:
                    fail_domains.setdefault(domain, []).append(
                        {"url": url, "status": r["status"], "detail": r.get("detail", "")}
                    )

    layer_stats = {}
    for r in url_result.values():
        layer = r.get("layer", "unknown")
        layer_stats[layer] = layer_stats.get(layer, 0) + 1
    if config.enable_ffprobe and url_result:
        fast_ok = layer_stats.get("fast_ok", 0)
        ratio = fast_ok / len(url_result)
        if ratio > 0.9:
            logger.warning(
                f"[告警] {ratio:.0%} 的源只通过 HTTP 层，ffprobe 可能未生效"
                "（检查 ffmpeg/ffprobe 是否可用、ffmpeg_path 是否为系统 PATH）"
            )
    logger.info(
        "质量检测完成（唯一 URL）："
        + "，".join(f"{k}={v}" for k, v in sorted(layer_stats.items()))
        + f"；频道-URL 组合={combos}"
    )
    return results, fail_domains


def filter_dead_urls(channels, check_results):
    """
    根据检测结果过滤失效源，返回去重后的 channels。
    保留条件: status 为 "ok" 或 "ok_no_ts"；默认 layer 需为 "ffprobe" 或 "deep"（排除仅快筛无元数据的源）。
    当 config.keep_http_passed = True 时，HTTP 快筛通过的源（layer="fast"）也会被保留，
    适用于组播/rtp 流等 ffprobe 难以读到元数据的场景。
    """
    keep_http = getattr(config, "keep_http_passed", False)
    keep_timeout = getattr(config, "keep_timeout_sources", False)
    filtered = {}
    for cat, ch_dict in channels.items():
        filtered[cat] = {}
        for ch_name, url_list in ch_dict.items():
            valid = []
            for url in url_list:
                cl = _strip_suffix(url)
                bucket = check_results.get(cat, {}).get(ch_name, {})
                r = bucket.get(url) or bucket.get(cl) or {}
                ok_status = r.get("status") in ("ok", "ok_no_ts")
                is_timeout = r.get("status") == "timeout"
                if ok_status and (keep_http or r.get("layer") in ("ffprobe", "deep")):
                    valid.append(url)
                elif is_timeout and keep_timeout:
                    valid.append(url)
            if valid:
                filtered[cat][ch_name] = valid

    removed = 0
    for c in filtered:
        for n in filtered.get(c, {}):
            removed += len(channels[c].get(n, [])) - len(filtered[c][n])
    kept = sum(len(urls) for c in filtered for urls in filtered[c].values())
    logger.info(f"过滤后: 保留 {kept} 个有效源，移除 {removed} 个失效源")
    return filtered
