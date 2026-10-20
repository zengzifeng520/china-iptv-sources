# -*- coding: utf-8 -*-
"""多人订阅用户管理（GitHub Pages 专属文件方案）

每个用户一个专属订阅文件（以 .m3u 结尾），到期自动失效：
    https://zengzifeng.ccwu.cc/output/users/张三_2027-01-01.m3u

NAS 每次 push_nas.py 推送时：
    - 未过期用户 -> 生成专属文件并推送到 GitHub（地址以 .m3u 结尾，播放器可直接导入）
    - 已过期用户 -> 自动从 GitHub 删除专属文件（播放器 404，打不开）

用法（在 NAS 上，项目目录内）:
    python3 gen_token.py --add 张三 --expire 2027-01-01    # 新增/续期一个用户
    python3 gen_token.py --list                             # 查看所有用户与订阅地址
    python3 gen_token.py --remove 张三                      # 删除用户（下次推送后失效）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
DEFAULT_BASE = "https://zengzifeng.ccwu.cc"  # 对外域名（GitHub Pages）


def load_users():
    if os.path.exists(USERS_FILE):
        try:
            return json.load(open(USERS_FILE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def expire_ts(date_str):
    """YYYY-MM-DD -> 到期时间戳（当天 23:59:59，含整天）。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.replace(hour=23, minute=59, second=59).timestamp())


def make_url(base, u, expire_date):
    """生成专属订阅地址：https://域名/output/users/<用户名>_<到期日期>.m3u
    以 .m3u 结尾，PotPlayer / IPTV 播放器识别为播放列表。"""
    from urllib.parse import quote
    base = base.rstrip("/")
    return f"{base}/output/users/{quote(u, safe='')}_{expire_date}.m3u"


def main():
    ap = argparse.ArgumentParser(description="多人订阅用户管理")
    ap.add_argument("--add", metavar="用户名", help="新增或续期用户")
    ap.add_argument("--expire", metavar="YYYY-MM-DD", help="到期日期（配合 --add）")
    ap.add_argument("--list", action="store_true", help="列出所有用户与订阅地址")
    ap.add_argument("--remove", metavar="用户名", help="删除用户")
    ap.add_argument("--base", default="", help="对外域名（默认 config.json 或 tingbao.kdns.fr）")
    args = ap.parse_args()

    base = args.base
    if not base:
        try:
            cfg = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
            base = cfg.get("base_url", "")
        except Exception:  # noqa: BLE001
            base = ""
    if not base:
        base = DEFAULT_BASE

    if args.remove:
        users = load_users()
        if args.remove in users:
            del users[args.remove]
            save_users(users)
            print(f"已删除用户: {args.remove}（下次推送后订阅自动失效）")
        else:
            print(f"用户不存在: {args.remove}")
        return

    if args.add:
        if not args.expire:
            print("请用 --expire 指定到期日期，例如: python3 gen_token.py --add 张三 --expire 2027-01-01")
            sys.exit(1)
        users = load_users()
        users[args.add] = {"expire": args.expire, "ts": str(expire_ts(args.expire))}
        save_users(users)
        print(f"用户 [{args.add}] 到期 {args.expire}")
        print(f"订阅地址（发给用户，导入播放器即可）:")
        print(make_url(base, args.add, args.expire))
        return

    # --list（默认也打印）
    users = load_users()
    now = int(time.time())
    print(f"域名: {base}")
    print(f"用户数: {len(users)}")
    print("-" * 40)
    for name, info in sorted(users.items()):
        e = str(info.get("ts", ""))
        expired = "已过期" if e and int(e) < now else f"剩余 {(int(e) - now) // 86400} 天"
        print(f"[{name}] 到期 {info.get('expire')}（{expired}）")
        print(f"  {make_url(base, name, info.get('expire'))}")
    if not users:
        print("（暂无用户，用 --add 添加）")


if __name__ == "__main__":
    main()
