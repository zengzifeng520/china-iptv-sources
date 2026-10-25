// Pages Function: /gw/<用户>/<到期时间戳>/<签名>.m3u
// 签名 = HMAC-SHA256(SUB_KEY, "用户名|到期时间戳")，到期或签名错返回 403。
// 需要在 Pages 项目 设置->环境变量 里配置 SUB_KEY。

async function hmacHex(key, msg) {
  const enc = new TextEncoder();
  const k = await crypto.subtle.importKey("raw", enc.encode(key),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, enc.encode(msg));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const m = url.pathname.match(/^\/gw\/([^/]+)\/(\d+)\/([0-9a-f]{64})\.m3u$/);
  if (!m) return new Response("Not Found", { status: 404 });

  let u;
  try { u = decodeURIComponent(m[1]); } catch (err) { u = m[1]; }
  const e = m[2], s = m[3];

  const SUB_KEY = env.SUB_KEY || "";
  if (!SUB_KEY) return new Response("Gateway not configured", { status: 500 });

  const expect = await hmacHex(SUB_KEY, `${u}|${e}`);
  if (s !== expect) return new Response("Forbidden", { status: 403 });

  const exp = parseInt(e, 10);
  if (!Number.isFinite(exp) || exp * 1000 < Date.now()) {
    return new Response("Forbidden (expired)", { status: 403 });
  }

  const UPSTREAM = env.UPSTREAM || "https://zengzifeng.ccwu.cc/output/gateway.m3u";
  let resp;
  try {
    resp = await fetch(UPSTREAM, { headers: { "User-Agent": "china-iptv-gw/1.0" } });
  } catch (err) {
    return new Response("Upstream error", { status: 502 });
  }
  if (!resp.ok) return new Response("Upstream unavailable", { status: 502 });
  const body = await resp.text();
  return new Response(body, {
    headers: {
      "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
      "Cache-Control": "no-store, no-cache, must-revalidate",
    },
  });
}
