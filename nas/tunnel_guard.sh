#!/bin/sh
# tunnel_guard.sh —— 保证 Cloudflare 隧道 + 心跳服务在跑；隧道地址变了就自动重写订阅
cd /vol1/1000/docker/china-iptv-sources || exit 1

# 1) 心跳服务
if ! pgrep -f "python3 -u track.py" >/dev/null 2>&1; then
    setsid nohup python3 -u track.py >> track.log 2>&1 < /dev/null &
fi

# 2) 隧道
if ! pgrep -f "cloudflared tunnel --url" >/dev/null 2>&1; then
    setsid nohup bin/cloudflared tunnel --url http://127.0.0.1:8898 --no-autoupdate         --logfile tunnel.log >> tunnel.out 2>&1 < /dev/null &
    sleep 15
fi

# 3) 地址变化 -> 写文件并重新生成用户订阅
NEW=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log 2>/dev/null | tail -1)
OLD=$(cat tunnel_url.txt 2>/dev/null)
if [ -n "$NEW" ] && [ "$NEW" != "$OLD" ]; then
    echo "$NEW" > tunnel_url.txt
    echo "$(date '+%F %T') 隧道地址 -> $NEW" >> tunnel_guard.log
    python3 -u sync_users.py >> users_sync.log 2>&1
fi
