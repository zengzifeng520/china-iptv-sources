#!/bin/sh
# 每小时触发 GitHub Actions 的 Hourly Check（GitHub 自带 cron 的冗余保险）
# token 单独存放：.github_token_dispatch（只给 Actions 用，不要和 push_nas.py 共用）
TOKEN=$(tr -d '\r\n' < /vol1/1000/docker/china-iptv-sources/.github_token_dispatch)
if [ -z "$TOKEN" ]; then
  echo "$(date '+%F %T') token 为空，退出"
  exit 1
fi

for repo in IPTV IPTVZB; do
  code=$(/usr/bin/curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -H "User-Agent: nas-cron" \
    "https://api.github.com/repos/zengzifeng520/$repo/actions/workflows/hourly-check.yml/dispatches" \
    -d '{"ref":"main"}')
  echo "$(date '+%F %T') $repo -> HTTP $code"
done
