# china-iptv-sources

自动采集 **全国酒店直播源** + **央视官方直播源**，并发检测有效性后生成 **TXT / M3U 订阅文件**，并借助 GitHub Actions 定时自动更新。把仓库里的 `output/live.txt` 和 `output/live.m3u` 地址填入任意播放器即可直接观看。

## 功能特性

- 📡 **酒店直播源采集**：默认接入 `ssili126/tv`（全国聚合 + 分省酒店源）、`fanmingming/live`（ITV 源）、`maowei1125/spider-iptv`（酒店/组播源）等多个公开数据源，支持在 `config.json` 中任意增删源与镜像地址
- 🏛️ **央视官方源采集**：从央视播放器（tv.cctv.com）内置的 12 个 CDN 域名按「频道 ID × CDN」模式构建候选地址，覆盖 CCTV-1~17、新闻、纪录、少儿、4K 等 21 个频道
- ✅ **有效性检测**：多线程并发 HTTP 探测，识别 m3u8 / FLV / TS 流，自动剔除失效源
- 📄 **双格式输出**：TVBox 兼容 TXT（`#分类,#genre#`）+ 标准 M3U，另附 `stats.json` 统计
- 🔄 **自动更新**：GitHub Actions 每 6 小时运行一次，检测到变化自动提交，订阅地址永远指向最新内容
- 🐍 **零依赖**：纯 Python 标准库实现，本地和云端开箱即用

## 订阅地址

部署完成后，将下面的地址填入任意支持 IPTV 的播放器（PotPlayer、VLC、电视盒子等）即可观看：

| 格式 | 订阅地址 |
| --- | --- |
| M3U | `https://raw.githubusercontent.com/zengzifeng520/china-iptv-sources/main/output/live.m3u` |
| M3U（jsDelivr CDN，国内更快） | `https://cdn.jsdelivr.net/gh/zengzifeng520/china-iptv-sources@main/output/live.m3u` |
| TXT | `https://raw.githubusercontent.com/zengzifeng520/china-iptv-sources/main/output/live.txt` |
| TXT（jsDelivr CDN） | `https://cdn.jsdelivr.net/gh/zengzifeng520/china-iptv-sources@main/output/live.txt` |

> 推荐使用 jsDelivr 地址：国内访问快、有 CDN 缓存。

## 项目结构

```
china-iptv-sources/
├── run.py                     # 入口：采集 → 校验 → 生成
├── config.json                # 数据源与校验参数配置
├── iptv_sources/
│   ├── fetcher.py             # HTTP 抓取（多镜像自动回退）
│   ├── parser.py              # TXT / M3U 解析
│   ├── hotel.py               # 酒店源采集与去重
│   ├── cctv.py                # 央视源构建与校验
│   ├── validator.py           # 并发有效性检测
│   └── generator.py           # 生成 live.txt / live.m3u / stats.json
├── .github/workflows/update.yml  # 自动更新工作流
└── output/                    # 生成的订阅文件（提交到仓库）
    ├── live.txt
    ├── live.m3u
    └── stats.json
```

## 本地运行

```bash
# 无需安装任何依赖（纯标准库）
python run.py
```

运行完成后查看 `output/` 下的文件，日志会输出各数据源抓取结果、有效/失效统计。

### 自定义配置（config.json）

```jsonc
{
  "hotel_sources": [
    {
      "name": "任意酒店源",
      "urls": [
        "https://raw.githubusercontent.com/xxx/yyy/main/酒店源.txt",   // 主地址
        "https://ghproxy.net/https://raw.githubusercontent.com/xxx/yyy/main/酒店源.txt"  // 镜像
      ]
    }
  ],
  "cctv": { "max_urls_per_channel": 3 },   // 每个央视频道最多保留的可用源数量
  "validator": { "timeout": 8, "workers": 40, "retries": 1 },  // 检测超时(秒)/并发数/重试次数
  "output_dir": "output",
  "max_lines_per_source": 2000,      // 单个数据源最大行数
  "max_hotel_candidates": 3000       // 酒店源候选总条数上限（防校验耗时过长）
}
```

- 支持 TXT（`频道名,url` + `#分类,#genre#`）与 M3U 两种源格式，自动识别
- 每个源可配置多个地址，抓取时按顺序回退（主源失败自动走镜像，适合国内网络）
- 所有 GitHub 源还会自动追加 `api.github.com` 备用通道，即使 raw 域名被墙也能抓到
- 酒店源中可能包含 `rtmp://`、`udp://` 等地址，解析时自动保留

## 网络与校验环境说明

- **央视源**为官方 CDN，国内外均可直连，校验稳定。
- **酒店源**多为国内运营商/酒店 IP 直连地址，可用性与**运行环境所在网络**强相关：
  - 在**国内网络**本地运行 `python run.py`，结果最贴近国内实际可用性；
  - 在 **GitHub Actions**（海外服务器）上运行，会过滤掉大部分国内专网源，这是网络差异的正常表现，并非程序问题。
- 因此建议：以 GitHub Actions 保证订阅地址定期刷新与基础可用性；如需更全的国内源，可自行在本机（或国内服务器）运行并推送结果。

## GitHub Actions 自动更新

推送代码到 GitHub 后，仓库的 **Actions** 会：

1. 每 6 小时（`cron: "0 */6 * * *"`）自动运行一次 `python run.py`
2. 重新抓取酒店源、重建央视源并全部校验
3. 结果有变化时自动提交 `output/` 到仓库（订阅地址无需改动，始终指向最新）

也可以随时在 Actions 页面点击 **Run workflow** 手动触发，或直接推送代码触发。

## 工作原理

```
酒店直播源(ssili126/fanmingming等) ──► 抓取(TXT/M3U解析, 跨源去重)
                                                 │
央视官方源(频道ID × 12个CDN模式) ────────────────┤
                                                 ▼
                              并发有效性检测(m3u8/FLV/TS识别)
                                                 ▼
                         生成 live.txt / live.m3u / stats.json
                                                 ▼
                        GitHub Actions 定时提交 ──► 订阅地址自动更新
```

## 免责声明

本项目仅供个人学习与交流使用。直播源内容版权归各电视台及版权方所有，请勿将本项目用于任何商业用途或传播侵权内容。酒店直播源来自公开网络数据，可能随时失效，本项目不对其可用性负责。

## License

[MIT](LICENSE)
