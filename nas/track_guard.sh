#!/bin/sh
# track_guard.sh —— 保证 track.py 一直在跑（挂了或被重启后自动拉起）
cd /vol1/1000/docker/china-iptv-sources || exit 1
if ! pgrep -f "python3 -u track.py" >/dev/null 2>&1; then
    setsid nohup python3 -u track.py >> track.log 2>&1 < /dev/null &
    echo "$(date '+%F %T') track.py 已重启" >> track.log
fi
