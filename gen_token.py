# -*- coding: utf-8 -*-
"""多人订阅用户管理 + 签名订阅地址生成（与 Cloudflare Worker 网关配套）。

用法（在 NAS 上，项目目录内）:
    python3 gen_token.py --add 张三 --expire 2027-01-01    # 新增/续期一个用户
    python3 gen_token.py --list                             # 查看所有用户与订阅地址
    python3 gen_token.py --remove 张三                      # 删除用户（订阅立即失效）
    python3 gen_token.py --expire <YYYY-MM-DD>              # 到期时间戳转秒（辅助）

工作方式:
    - 用户清单存本地 users.json（不推送 GitHub）
    - 订阅地址 = GATEWAY_URL + ?u=<用户名>&e=<到期时间戳>&s=<HMAC签名>
    - 签名 = HMAC-SHA256(密钥, "u|e")，密钥在 .gateway_key（与 Worker 的 SUB_KEY 一致）
    - 到期后 Worker 自动拒绝，无需手动删除
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
KEY_FILE = os.path.join(BASE_DIR, ".gateway_key")
DEFAULT_GATEWAY = "https://tv.tingbao.kdns.fr/live.m3u"  # Worker 绑定域名后的订阅入口


def load_key():
    """读取网关密钥；不存在则生成一个（写入 .gateway_key，不推送 GitHub）。"""
    if os.path.exists(KEY_FILE):
        k = open(KEY_FILE, encoding="utf-8").read().strip()
        if k:
            return k
    k = "gw-" + hashlib.sha256(os.urandom(32)).hexdigest()
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(k)
    print(f"已生成网关密钥 -> .gateway_key（请把它填入 Cloudflare Worker 的环境变量 SUB_KEY）")
    print(f"密钥内容: {k}")
    return k


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


def sign(key, u, e):
    return hmac.new(key.encode("utf-8"), f"{u}|{e}".encode("utf-8"), hashlib.sha256).hexdigest()


def make_url(gw, key, u, e):
    s = sign(key, u, e)
    return f"{gw}?u={u}&e={e}&s={s}"


def main():
    ap = argparse.ArgumentParser(description="多人订阅用户管理")
    ap.add_argument("--add", metavar="用户名", help="新增或续期用户")
    ap.add_argument("--expire", metavar="YYYY-MM-DD", help="到期日期（配合 --add）")
    ap.add_argument("--list", action="store_true", help="列出所有用户与订阅地址")
    ap.add_argument("--remove", metavar="用户名", help="删除用户")
    ap.add_argument("--gateway", default="", help="网关地址（默认用 config.json 或内置默认值）")
    args = ap.parse_args()

    key = load_key()
    # 网关地址：命令行 > config.json > 默认
    gw = args.gateway
    if not gw:
        try:
            cfg = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
            gw = cfg.get("gateway_url", "")
        except Exception:  # noqa: BLE001
            gw = ""
    if not gw:
        gw = DEFAULT_GATEWAY

    if args.remove:
        users = load_users()
        if args.remove in users:
            del users[args.remove]
            save_users(users)
            print(f"已删除用户: {args.remove}（订阅立即失效）")
        else:
            print(f"用户不存在: {args.remove}")
        return

    if args.add:
        if not args.expire:
            print("请用 --expire 指定到期日期，例如: python3 gen_token.py --add 张三 --expire 2027-01-01")
            sys.exit(1)
        users = load_users()
        e = str(expire_ts(args.expire))
        users[args.add] = {"expire": args.expire, "ts": e}
        save_users(users)
        print(f"用户 [{args.add}] 到期 {args.expire}")
        print(f"订阅地址（发给用户，导入播放器即可）:")
        print(make_url(gw, key, args.add, e))
        return

    # --list（默认也打印）
    users = load_users()
    now = int(time.time())
    print(f"网关: {gw}")
    print(f"用户数: {len(users)}")
    print("-" * 40)
    for name, info in sorted(users.items()):
        e = str(info.get("ts", ""))
        expired = "已过期" if e and int(e) * 1000 < now * 1000 else f"剩余 {(int(e) - now) // 86400} 天"
        print(f"[{name}] 到期 {info.get('expire')}（{expired}）")
        print(f"  {make_url(gw, key, name, e)}")
    if not users:
        print("（暂无用户，用 --add 添加）")


if __name__ == "__main__":
    main()
