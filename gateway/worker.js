// -*- coding: utf-8 -*-
/* Cloudflare Worker：多人订阅签名网关
 *
 * 作用：
 *   - 订阅地址必须是「带签名 + 带到期时间」的 URL 才能播放
 *   - 签名由你的密钥（环境变量 SUB_KEY）HMAC-SHA256 生成
 *   - 到期后自动返回 403，无需手动删除用户
 *   - 所有用户看到同一份 live.m3u（内容由 UPSTREAM 指定）
 *
 * 部署：
 *   1. cloudflare.com 注册 -> Workers & Pages -> 创建 Worker
 *   2. 把本文件内容粘贴进编辑器
 *   3. 设置 -> 变量和机密（生产环境）：
 *        SUB_KEY  = 你的随机密钥（与 NAS 上 .gateway_key 内容一致）
 *        UPSTREAM = https://tingbao.kdns.fr/output/live.m3u
 *   4. 保存版本 -> 部署（必须重新部署，变量才会生效）
 *
 * 签名生成（NAS 上）：
 *   python3 gen_token.py --add 张三 --expire 2027-01-01
 */

const encoder = new TextEncoder();

async function hmacHex(key, msg) {
  const k = await crypto.subtle.importKey(
    "raw", encoder.encode(key),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", k, encoder.encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // ---- 路径型（推荐）：https://网关/<用户>/<到期时间戳>/<签名>.m3u ----
    // 地址以 .m3u 结尾，PotPlayer / IPTV 播放器都能识别为播放列表
    let u = "", e = "", s = "";
    const m = url.pathname.match(/^\/([^/]+)\/(\d+)\/([0-9a-f]{64})\.m3u$/);
    if (m) {
      try { u = decodeURIComponent(m[1]); } catch (err) { u = m[1]; }
      e = m[2];
      s = m[3];
    } else {
      // ---- 兼容旧式 query 型：?u=&e=&s= ----
      u = url.searchParams.get("u") || "";
      e = url.searchParams.get("e") || "";
      s = url.searchParams.get("s") || "";
    }

    // 1) 参数齐全性
    if (!u || !e || !s) {
      return new Response("Not Found", { status: 404 });
    }

    // 2) 签名校验（变量从 env 读取，兼容 globalThis）
    const SUB_KEY = env.SUB_KEY || globalThis.SUB_KEY || "";
    if (!SUB_KEY) {
      return new Response("Gateway not configured", { status: 500 });
    }
    const expect = await hmacHex(SUB_KEY, `${u}|${e}`);
    if (s !== expect) {
      return new Response("Forbidden", { status: 403 });
    }

    // 3) 到期校验
    const exp = parseInt(e, 10);
    if (!Number.isFinite(exp) || exp * 1000 < Date.now()) {
      return new Response("Forbidden (expired)", { status: 403 });
    }

    // 4) 拉取上游订阅（GitHub Pages 上的 live.m3u）
    const UPSTREAM = env.UPSTREAM || globalThis.UPSTREAM || "";
    if (!UPSTREAM) {
      return new Response("Upstream not configured", { status: 500 });
    }
    let resp;
    try {
      resp = await fetch(UPSTREAM, {
        headers: { "User-Agent": "china-iptv-gateway/1.0", "Accept": "*/*" },
      });
    } catch (err) {
      return new Response("Upstream error", { status: 502 });
    }
    if (!resp.ok) {
      return new Response("Upstream unavailable", { status: 502 });
    }
    const body = await resp.text();

    // 5) 返回 M3U（禁止缓存，保证每次都是最新）
    return new Response(body, {
      headers: {
        "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
      },
    });
  },
};
