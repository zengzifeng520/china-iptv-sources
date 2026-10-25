#!/bin/sh
# 每天 05:00 / 15:00 触发 IPTVZB 的 Multicast Update（GitHub 自带 cron 不可靠，用 NAS 兜底）
TOKEN=$(tr -d '\r\n' < /vol1/1000/docker/china-iptv-sources/.github_token_dispatch)
if [ -z "$TOKEN" ]; then
  echo "$(date '+%F %T') token 为空，退出"
  exit 1
fi

code=$(/usr/bin/curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "User-Agent: nas-cron" \
  "https://api.github.com/repos/zengzifeng520/IPTVZB/actions/workflows/mcast.yml/dispatches" \
  -d '{"ref":"main"}')
echo "$(date '+%F %T') IPTVZB/Multicast -> HTTP $code"
