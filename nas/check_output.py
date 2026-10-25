# -*- coding: utf-8 -*-
"""云端 Actions 检查脚本：只校验 output 订阅文件的格式与内容，不做连通性过滤。

为什么不做连通性校验：NAS 推送的直播源多为国内可访问地址，
GitHub Actions 海外服务器访问不到，若做连通性校验会误删全部条目。
本脚本仅检查 output 是否非空、格式是否正确，异常时以非零退出码
触发 Actions 失败告警（可在 workflow 中接续发通知）。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


def main():
    problems = []

    txt = os.path.join(OUTPUT_DIR, "live.txt")
    m3u = os.path.join(OUTPUT_DIR, "live.m3u")
    stats = os.path.join(OUTPUT_DIR, "stats.json")

    if not os.path.exists(txt):
        problems.append("缺少 output/live.txt（NAS 尚未推送？）")
    if not os.path.exists(m3u):
        problems.append("缺少 output/live.m3u（NAS 尚未推送？）")

    txt_count = 0
    if os.path.exists(txt):
        with open(txt, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and "," in line and not line.endswith("#genre#"):
                    txt_count += 1
        if txt_count == 0:
            problems.append("live.txt 中没有有效条目")
        else:
            print(f"live.txt 有效条目: {txt_count}")

    m3u_count = 0
    if os.path.exists(m3u):
        with open(m3u, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    m3u_count += 1
        if m3u_count == 0:
            problems.append("live.m3u 中没有有效条目")
        else:
            print(f"live.m3u 有效条目: {m3u_count}")

    if os.path.exists(stats):
        try:
            st = json.load(open(stats, encoding="utf-8"))
            print("stats.json: updated_at =", st.get("updated_at"),
                  "| total =", st.get("total"), "| via =", st.get("via", "?"))
        except Exception:  # noqa: BLE001
            problems.append("stats.json 无法解析")

    if problems:
        print("检查未通过：")
        for p in problems:
            print("  -", p)
        print("提示：直播源由本地/NAS 的 push_nas.py 定时推送，请检查 NAS 上的定时任务是否正常运行。")
        sys.exit(1)
    print("output 检查通过。")


if __name__ == "__main__":
    main()
