#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_sub.py —— 把 NAS 本地订阅发布到 GitHub，供外网观看。

源： http://127.0.0.1:8888/sub.m3u      （local_sub.py 常驻服务）
产物：output/nas.m3u                    （公网 CDN 可直接订阅）

频道地址本身是中转的公网 IP，所以外网拉下来就能播；
本地服务只负责在群播源/中转变化时自动跟上。
"""
import os
import sys
import base64
import hashlib
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from push_nas import DEFAULT_REPO, load_token, push_file  # noqa: E402

SRC = os.environ.get("NAS_SUB_URL", "http://127.0.0.1:8888/sub.m3u")
PATH = os.environ.get("NAS_SUB_PATH", "output/nas.m3u")
MIN_ITEMS = int(os.environ.get("NAS_SUB_MIN", "10"))
STATE = os.path.join(BASE, "data", "publish_state.sha")


def body_hash(data: bytes) -> str:
    """只对有意义的行做哈希：忽略 # 开头的注释行（里面有每次生成的时间戳）。
    这样中转没变、频道没变时不会产生无谓的提交。"""
    keep = []
    for raw in data.decode("utf-8", "ignore").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        keep.append(s)
    # 排序后再算哈希：只要「地址集合」没变就不推，避免因码率波动改变线路顺序而频繁提交
    keep.sort()
    return hashlib.sha1("\n".join(keep).encode("utf-8")).hexdigest()


def last_hash() -> str:
    try:
        return open(STATE, encoding="utf-8").read().strip()
    except OSError:
        return ""


def main():
    try:
        with urllib.request.urlopen(SRC, timeout=20) as r:
            data = r.read()
    except Exception as e:  # noqa: BLE001
        print("拉取本地订阅失败：%s" % e)
        return 1

    n = data.count(b"#EXTINF")
    if n < MIN_ITEMS:
        print("条目过少（%d），不发布，避免把空列表推上去" % n)
        return 1

    h = body_hash(data)
    if h == last_hash():
        print("内容无变化（%d 条），跳过推送" % n)
        return 0

    try:
        tok = load_token()
        push_file(DEFAULT_REPO, PATH, data, tok)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print("推送失败：%s" % e)
        return 1

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    open(STATE, "w", encoding="utf-8").write(h)
    print("已发布 %d 条 -> %s（变更）" % (n, PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
