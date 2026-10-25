#!/bin/sh
# daily_multicast.sh —— 每天 6:00 / 17:00：爬 foodieguide 组播源 → 合并进最新源 → 校验/台标/EPG → 推送
set -u
cd /vol1/1000/docker/china-iptv-sources || exit 1
SRC_DIR=/vol1/1000/docker/iptv-manager/data/uploads
MERGED=/tmp/live_mc_merged.m3u
STAMP=$(date '+%F %T')

echo "==== $STAMP 开始 ====" >> crawl.log
SRC=$(ls -1t "$SRC_DIR"/*.m3u "$SRC_DIR"/*.txt 2>/dev/null | head -n 1)
if [ -z "${SRC:-}" ] || [ ! -f "$SRC" ]; then
    echo "$STAMP 找不到源文件：$SRC_DIR，退出" >> crawl.log
    exit 1
fi
echo "$STAMP 源文件：$SRC" >> crawl.log

python3 -u crawl_multicast.py --workers 3 --interval 0.8 --max-rounds 150 \
    --merge "$SRC" -o "$MERGED" >> crawl.log 2>&1
RC_CRAWL=$?
echo "$STAMP 爬取退出码 $RC_CRAWL" >> crawl.log

if [ -s "$MERGED" ]; then
    python3 -u push_nas.py --file "$MERGED" --validate --sync-epg --sync-logo >> push.log 2>&1
else
    echo "$STAMP 合并文件为空，退回原源推送" >> crawl.log
    python3 -u push_nas.py --latest-dir "$SRC_DIR" --validate --sync-epg --sync-logo >> push.log 2>&1
fi
RC_PUSH=$?
echo "$STAMP 推送退出码 $RC_PUSH；结束 ====" >> crawl.log
