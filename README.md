# china-iptv-sources

> 把 **飞牛 NAS / 本地服务器上维护的直播源** 自动推送到 GitHub，生成 **TXT / M3U 订阅地址**，每个频道自动匹配**台标（tvg-logo）与节目信息（tvg-id + EPG）**。

你只需要在 NAS 上维护一份直播源文件（`m3u` 或 `txt`），本工具负责把它**定时同步到 GitHub**，订阅地址永远指向最新内容，播放器直接填入即可。

---

## 📺 订阅地址

| 格式 | 订阅地址 |
| --- | --- |
| **M3U（推荐，含台标+节目单）** | `https://raw.githubusercontent.com/zengzifeng520/china-iptv-sources/main/output/live.m3u` |
| M3U（你的域名） | `https://tingbao.kdns.fr/output/live.m3u` |
| M3U（jsDelivr CDN 加速） | `https://cdn.jsdelivr.net/gh/zengzifeng520/china-iptv-sources@main/output/live.m3u` |
| TXT（TVBox 格式） | `https://raw.githubusercontent.com/zengzifeng520/china-iptv-sources/main/output/live.txt` |
| TXT（jsDelivr CDN 加速） | `https://cdn.jsdelivr.net/gh/zengzifeng520/china-iptv-sources@main/output/live.txt` |

> **M3U 订阅特点**（每一条播放地址都自动带台标与节目信息）：
> - 每个频道自动匹配 **台标**（`tvg-logo`），实测覆盖率 **96%+**
> - 每个频道带 **节目单标识**（`tvg-id` / `tvg-name`），覆盖率 **100%**
> - 订阅头部自动挂载 **自托管合并节目单（EPG）**：`alantang1977`（央视/卫视）+ `CCSH`（505 频道大覆盖聚合）双源合并，约 500 个频道、18000+ 条节目，播放器可直接显示当日节目

---

## 👥 多人订阅（每人一个地址，可控制到期时间）

想分享给家人/朋友使用，又不想地址被无限转发？每个用户拿到**专属订阅文件**，**到期自动失效**，无需手动删除；续期重新生成即可。

```
https://tingbao.kdns.fr/output/users/张三_2027-01-01.m3u
```

**工作原理**：NAS 每次推送时，为每个未过期用户生成一份专属订阅文件（内容与 live.m3u 相同），文件名的日期就是到期日：

- 未到期 → 专属文件在 GitHub 上，播放器可正常导入（地址以 `.m3u` 结尾，PotPlayer / TiviMate 等直接识别为播放列表）
- 已到期 → NAS 自动把该用户的专属文件从 GitHub 删除，播放器 404 打不开
- 续期/改日期 → 自动生成新文件并删除旧文件，旧地址失效

### NAS 上管理用户（推送后自动生成/删除专属文件）

```bash
cd /vol1/1000/docker/china-iptv-sources

# 新增用户（到期 2027-01-01，输出订阅地址发给对方）
python3 gen_token.py --add 张三 --expire 2027-01-01

# 查看所有用户（含每个用户的完整订阅地址，已过期的会标注）
python3 gen_token.py --list

# 续期（重新执行 --add 即可，旧地址失效，需把新地址发给对方重新导入）
python3 gen_token.py --add 张三 --expire 2027-06-01

# 删除用户（下次推送后订阅自动失效）
python3 gen_token.py --remove 张三
```

> 用户清单保存在 NAS 本地 `users.json`（不推送 GitHub）。每次 `push_nas.py` 推送（`--latest-dir ... --validate --sync-epg --sync-logo`）会自动：①为未过期用户生成/更新专属文件并推送到 GitHub；②删除已到期用户的专属文件（订阅立即失效）。

---

## ✨ 台标与节目单（每次推送自动匹配）

推送时加上 `--sync-epg --sync-logo`，脚本会自动完成：

1. **下载最新节目单** → `output/epg.xml`（订阅头部自动挂载，播放器即可显示当日/一周节目）
2. **按频道名自动匹配台标** → 下载到 `output/logo/`（M3U 每行自动带 `tvg-logo`）
3. **台标增量推送** → 记录文件 sha256，只推变更的台标，不重复上传

**台标匹配链**（四级，实测 1955 条真实源覆盖率 96.4%）：

```
精确匹配（CCTV-1综合 → CCTV1.png）
  → 规范化匹配（繁简互转、空格/大小写归一，如 TVB DRAMA → tvbdrama）
  → 子串包含（如 Now剧集 → Now剧集台）
  → 中文模糊（如 湖南卫视高清 → 湖南卫视）
```

**数据源**（全部为持续维护的活跃仓库，无需你维护）：
- 节目单：`alantang1977/iptv_api`（每天更新）+ `CCSH/IPTV`（505 频道聚合大源），自动合并去重后统一挂载
- 台标库：`Meroser/Media_Coll`（活跃）+ `fanmingming/live tv/`（929 个中文台标，静态图片不受仓库停更影响）+ `Meroser/IPTV`

```bash
python3 push_nas.py --latest-dir "/vol1/1000/docker/iptv-manager/data/uploads" --validate --sync-epg --sync-logo
```

---

## 🚀 在飞牛 NAS 上部署（推荐：部署包方式）

### 第 1 步：把项目放到 NAS

在电脑上下载部署包 `china-iptv-sources-nas.tar.gz`，用**飞牛 Web 文件管理器**上传到 NAS 的 `/vol1/1000/docker/` 目录，然后 SSH 终端执行：

```bash
cd /vol1/1000/docker
tar -xzf china-iptv-sources-nas.tar.gz -C /vol1/1000/docker/china-iptv-sources/
```

> 部署包内已包含全部代码、160+ 台标、节目单索引缓存；解压到项目目录 `china-iptv-sources/` 内（注意：包内文件不带顶层目录，必须解压到 `china-iptv-sources/` 本身，不要解压到 docker 根目录）。
>
> **也可以直接用 git 方式部署**：
> ```bash
> cd /vol1/1000/docker
> git clone https://github.com/zengzifeng520/china-iptv-sources.git
> ```

飞牛 fnOS 基于 Debian，一般自带 `python3`；若缺失：

```bash
sudo apt update && sudo apt install -y python3
```

### 第 2 步：配置推送令牌

把 GitHub 令牌写入项目目录下的 `.github_token` 文件（该文件已加入 `.gitignore`，**不会上传到仓库**）：

```bash
cd /vol1/1000/docker/china-iptv-sources
echo 'github_pat_xxxxxxxxxxxxxxxx' > .github_token
```

> - 令牌在 GitHub 设置 → Developer settings → Personal access tokens 创建，需要 **Contents 读写权限**（仓库 `zengzifeng520/china-iptv-sources`）
> - 也可改用环境变量：`export GITHUB_TOKEN=xxx`
> - 部署包内**不包含**令牌文件（安全考虑），覆盖部署时保留原来的 `.github_token` 即可

### 第 3 步：检查源文件路径配置

编辑 `config.json`，确认 `nas.source_file` 是你 NAS 上直播源文件的真实路径（m3u 或 txt）：

```jsonc
{
  "nas": {
    "source_file": "/vol1/1000/docker/iptv-manager/data/uploads/live.m3u"
  },
  "base_url": "https://tingbao.kdns.fr",
  "validator": { "workers": 20 }
}
```

> 如果源文件**名字每次会变**（如 iptv-manager 每次生成带随机名的新文件），不用改路径，直接使用 `--latest-dir` 参数自动选择目录中最新的 m3u/txt（见下文定时任务），一劳永逸。

### 第 4 步：设置定时任务（每天自动检测 + 推送）

```bash
crontab -e
```

添加以下三行（**每天两轮：检测 → 更新源 → 推送**）：

```
0 5,17 * * * cd /vol1/1000/docker/china-iptv-sources && python3 -u push_nas.py --latest-dir /vol1/1000/docker/iptv-manager/data/uploads --validate --no-push >> push.log 2>&1
0 6,18 * * * cd /vol1/1000/docker/iptv-manager && bash sync_sub.sh >> /vol1/1000/docker/china-iptv-sources/push.log 2>&1
0 7,19 * * * cd /vol1/1000/docker/china-iptv-sources && python3 -u push_nas.py --latest-dir /vol1/1000/docker/iptv-manager/data/uploads --validate --sync-epg --sync-logo >> push.log 2>&1
```

| 时间 | 任务 | 说明 |
| --- | --- | --- |
| 5:00 / 17:00 | **检测** | 20 并发逐个探测直播源有效性（每条超时 4 秒），只检测不推送 |
| 6:00 / 18:00 | **更新源** | iptv-manager 同步最新直播源（若你的更新命令不同，替换第二行） |
| 7:00 / 19:00 | **推送** | 生成订阅（同步节目单 + 台标）+ 校验 + 推送到 GitHub |

> 加 `-u` 参数让日志实时写入（方便排查）；校验超时已优化为 4 秒，2 万条源约 15-60 分钟完成。

### 第 5 步：手动推送一次，验证全链路

```bash
cd /vol1/1000/docker/china-iptv-sources
nohup python3 -u push_nas.py --latest-dir /vol1/1000/docker/iptv-manager/data/uploads --validate --sync-epg --sync-logo > push_run.log 2>&1 &

# 查看实时进度（能看到 EPG 下载 → 图标同步 → 校验 → 推送）
tail -f push_run.log
```

看到 `已推送 live.m3u (xxx bytes)` 即完成，约 1 分钟后订阅地址刷新为最新内容。

---

## 🔧 手动推送常用命令

```bash
# 导入 NAS 源文件并推送（默认不校验，原样保留）
python3 push_nas.py --file "/vol1/IPTV/live.m3u"

# 源文件名字会变？用 --latest-dir 自动选最新文件
python3 push_nas.py --latest-dir "/vol1/1000/docker/iptv-manager/data/uploads"

# 推送前做三级有效性校验（master→分片全通过才保留）
python3 push_nas.py --file "/vol1/IPTV/live.m3u" --validate

# 统一覆盖分类名（默认保留文件内分组）
python3 push_nas.py --file "/vol1/IPTV/live.txt" --category "我的源"

# 只推送本地 output/（跳过导入）
python3 push_nas.py --push-only

# 只生成不推送（本地预览）
python3 push_nas.py --file "/vol1/IPTV/live.m3u" --no-push
```

---

## 📄 源文件格式说明

支持两种常见格式，自动识别（UTF-8 / GBK 编码均支持）：

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

> `http/https/rtmp/rtsp/udp/rtp` 地址均支持；组播/rtsp 源校验时自动保留，不误删。

---

## 🛠 项目结构

```
china-iptv-sources/
├── push_nas.py               # 入口：NAS 源文件 → 生成订阅（台标+节目单）→ 推送 GitHub
├── check_output.py           # Actions 用：output 轻量检查（非空、格式）
├── config.json               # NAS 源路径 / 域名 / EPG 数据源配置
├── iptv_sources/
│   ├── parser.py             # TXT / M3U 解析（自动识别编码）
│   ├── generator.py          # 生成 live.txt / live.m3u（tvg-id/tvg-logo/x-tvg-url）
│   ├── validator.py          # 有效性检测（三级校验，--validate 时使用）
│   └── fetcher.py            # HTTP 抓取工具
├── .github/workflows/update.yml  # Actions 检查工作流
└── output/                   # 生成的订阅文件（自动提交）
    ├── live.txt
    ├── live.m3u              # 头部挂载双 EPG，每个频道带台标
    ├── all.m3u               # --validate 时的全量兜底订阅
    ├── epg.xml               # 最新节目单（自动同步）
    ├── logo/                 # 频道台标（自动同步、增量推送）
    └── stats.json
```

---

## ❓ 常见问题

- **推送上去了但订阅没变化？** GitHub 有约 1 分钟同步延迟；jsDelivr 另有 CDN 缓存（强制刷新可加 `?v=时间戳`）。
- **`--validate` 后条目变少？** 说明部分源失效被过滤，属正常现象；不加 `--validate` 则原样推送。
- **校验很慢？** 每条超时 4 秒，2 万条源约 15-60 分钟；日志看不到进度是 python 缓冲导致，命令加 `-u` 即可实时看到。
- **台标没匹配上？** 少数小众台（咪咕专属付费台、个别英文/港澳台）台标库中不存在，属正常上限（覆盖率 96%+）；国内常见台全覆盖。
- **Actions 报错？** 一般是 `output/` 为空，检查 NAS 定时任务是否正常运行。

---

## ⚖️ 免责声明

本项目仅供个人学习与交流使用。直播源内容版权归各电视台及版权方所有，请勿用于商业用途或传播侵权内容。

## License

[MIT](LICENSE)
