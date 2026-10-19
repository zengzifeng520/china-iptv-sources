# china-iptv-sources

把 **飞牛 NAS / 本地服务器上维护的直播源** 自动推送到 GitHub，并生成 **TXT / M3U 订阅地址**。

你只需要在 NAS 上维护一份直播源文件（`m3u` 或 `txt`），本工具负责把它定时同步到 GitHub，订阅地址永远指向最新内容，播放器直接填入即可。

## 订阅地址

| 格式 | 订阅地址 |
| --- | --- |
| M3U | `https://raw.githubusercontent.com/zengzifeng520/china-iptv-sources/main/output/live.m3u` |
| M3U（jsDelivr CDN，国内更快） | `https://cdn.jsdelivr.net/gh/zengzifeng520/china-iptv-sources@main/output/live.m3u` |
| TXT | `https://raw.githubusercontent.com/zengzifeng520/china-iptv-sources/main/output/live.txt` |
| TXT（jsDelivr CDN） | `https://cdn.jsdelivr.net/gh/zengzifeng520/china-iptv-sources@main/output/live.txt` |

> 内容由你的 NAS 定时推送，首次部署后推送一次即生效。

## 在飞牛 NAS 上部署（三步）

### 1. 把项目放到 NAS

```bash
cd /vol1/iptv
git clone https://github.com/zengzifeng520/china-iptv-sources.git
# 或直接上传项目文件夹到 /vol1/iptv/china-iptv-sources
```

飞牛 fnOS 基于 Debian，一般自带 `python3`；若缺失：

```bash
sudo apt update && sudo apt install -y python3
```

### 2. 配置推送令牌

把 GitHub 令牌写入项目目录下的 `.github_token` 文件（该文件已加入 `.gitignore`，不会上传到仓库）：

```bash
cd /vol1/iptv/china-iptv-sources
echo 'github_pat_xxxxxxxxxxxxxxxx' > .github_token
```

> 令牌在 GitHub 设置 → Developer settings → Personal access tokens 创建，需要 **Contents 读写权限**（仓库 `zengzifeng520/china-iptv-sources`）。
> 也可改用环境变量：`export GITHUB_TOKEN=xxx`。

### 3. 配置源文件路径并设置定时推送

编辑 `config.json`，把 `nas.source_file` 改成你 NAS 上直播源文件的真实路径：

```jsonc
{
  "nas": {
    "source_file": "/vol1/IPTV/live.m3u"   // 改成你的实际路径（m3u 或 txt）
  }
}
```

设置 crontab 定时任务（每天 02:30 推送一次）：

```bash
crontab -e
# 添加一行：
30 2 * * * cd /vol1/iptv/china-iptv-sources && python3 push_nas.py --file "/vol1/IPTV/live.m3u" >> push.log 2>&1
```

想更频繁（每 6 小时）：

```bash
0 */6 * * * cd /vol1/iptv/china-iptv-sources && python3 push_nas.py --file "/vol1/IPTV/live.m3u" >> push.log 2>&1
```

## 手动推送

```bash
# 导入 NAS 源文件并推送（默认不校验，原样保留）
python3 push_nas.py --file "/vol1/IPTV/live.m3u"

# 源文件名字会变（如 iptv-manager 每次生成带随机名的新文件）？
# 用 --latest-dir 自动选择目录中最新的 m3u/txt，一劳永逸：
python3 push_nas.py --latest-dir "/vol1/1000/docker/iptv-manager/data/uploads"

# 推送前先做三级有效性校验（master→分片全通过才保留）
python3 push_nas.py --file "/vol1/IPTV/live.m3u" --validate

# 统一覆盖分类名（默认保留文件内分组）
python3 push_nas.py --file "/vol1/IPTV/live.txt" --category "我的源"

# 只推送本地 output/（跳过导入）
python3 push_nas.py --push-only

# 只生成不推送
python3 push_nas.py --file "/vol1/IPTV/live.m3u" --no-push
```

推送成功约 1 分钟后，上面的订阅地址即刷新为最新内容。

## 源文件格式说明

支持两种常见格式，自动识别：

**TXT（TVBox 格式）**
```
# 央视频道,#genre#
CCTV-1 综合,http://example.com/live/cctv1.m3u8
```

**M3U**
```
#EXTM3U
#EXTINF:-1 group-title="央视频道",CCTV-1 综合
http://example.com/live/cctv1.m3u8
```

> 文件编码支持 UTF-8 / GBK，自动识别；`http/https/rtmp/rtsp/udp/rtp` 地址均支持。

## GitHub Actions 自动检查

仓库的 Actions 每 6 小时运行一次**轻量检查**（`check_output.py`）：确认 `output/` 非空、格式正确。**不会覆盖 NAS 推送的订阅**，也不会做连通性过滤（NAS 源多为国内地址，海外服务器无法真实校验）。若 output 为空（例如 NAS 定时任务停摆），Actions 会失败告警提醒你检查 NAS。

## 项目结构

```
china-iptv-sources/
├── push_nas.py               # 入口：NAS 源文件 → 生成订阅 → 推送 GitHub
├── check_output.py           # Actions 用：output 轻量检查（非空、格式）
├── config.json               # NAS 源文件路径配置
├── iptv_sources/
│   ├── parser.py             # TXT / M3U 解析（自动识别）
│   ├── generator.py          # 生成 live.txt / live.m3u / stats.json
│   ├── validator.py          # 三级有效性校验（--validate 时使用）
│   └── fetcher.py            # HTTP 抓取工具
├── .github/workflows/update.yml  # Actions 检查工作流
└── output/                   # 生成的订阅文件（自动提交）
    ├── live.txt
    ├── live.m3u
    └── stats.json
```

## 常见问题

- **推送上去了但订阅没变化？** GitHub 有约 1 分钟同步延迟；jsDelivr 另有 CDN 缓存（强制刷新可加 `?v=时间戳`）。
- **`--validate` 后条目变少？** 说明部分源失效被过滤，属正常现象；不加 `--validate` 则原样推送。
- **Actions 报错？** 一般是 `output/` 为空，检查 NAS 定时任务是否正常运行。

## 免责声明

本项目仅供个人学习与交流使用。直播源内容版权归各电视台及版权方所有，请勿用于商业用途或传播侵权内容。

## License

[MIT](LICENSE)
