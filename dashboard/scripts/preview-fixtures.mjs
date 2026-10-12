// Explicit, loopback-only UI fixture server. Never import this into production.
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
if (!process.argv.includes("--fixtures"))
  throw new Error("Pass --fixtures to enable TEST DATA.");
const dist = fileURLToPath(new URL("../dist", import.meta.url));
const port = Number(process.env.DASHBOARD_PREVIEW_PORT || 4175);
const now = () => new Date().toISOString();
const uid = (n) => "00000000-0000-4000-8000-" + String(n).padStart(12, "0");
const admin = {
  id: uid(999),
  username: "preview",
  display_name: "مدیر آزمایشی",
  is_active: true,
  last_login_at: now(),
};
let authorized = true;
const users = [
  "مینا احمدی",
  "سینا رضایی",
  "علی کریمی",
  "سارا محمدی",
  "محمد حسینی",
  "آرمان نوری",
  "رضا مرادی",
  "زهرا صالحی",
].map((name, i) => ({
  id: uid(i + 1),
  first_name: name,
  username: "test_user_" + (i + 1),
  telegram_id: 710001 + i,
  role: i === 0 ? "admin" : "user",
  status: i === 5 ? "banned" : "active",
  balance_usd: 26.75 + i * 7.5,
  credit_limit_usd: 10,
  created_at: now(),
  last_seen_at: now(),
}));
const servers = ["تهران · 01", "آلمان · 02", "هلند · 03", "فنلاند · 04"].map(
  (name, i) => ({
    id: uid(100 + i),
    name,
    base_url: "https://node" + i + ".example.invalid/panel",
    panel_type: "sanaei_xui",
    is_active: true,
    priority: 100,
    health_status: ["healthy", "unhealthy", "healthy", "unknown"][i],
    subscription_port: 2096,
    config_domain: null,
    sub_domain: null,
    max_clients: [60, 40, 100, null][i],
    inbound_count: 4,
    active_inbound_count: 3,
    client_count: 44 + i * 5,
    active_client_count: [40, 37, 80, 6][i],
  }),
);
const receipts = users.slice(0, 3).map((u, i) => ({
  id: uid(300 + i),
  created_at: now(),
  price_amount_usd: 12 + i * 3,
  pay_amount: 1500000 + i * 350000,
  pay_currency: "IRT",
  status: "pending_approval",
  receipt_file_id: null,
  card_number: "6037990000000000",
  card_holder: "دارندهٔ آزمایشی",
  card_bank: "بانک آزمایشی",
  user: u,
}));
const points = Array.from({ length: 30 }, (_, i) => {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - 29 + i);
  return {
    date: d.toISOString().slice(0, 10),
    value: Math.round(55 + i * 3.4 + Math.sin(i * 0.8) * 25),
  };
});
const overview = () => ({
  generated_at: now(),
  kpis: {
    total_users: 8426,
    active_subs: 3281,
    revenue_mtd_usd: 9247.5,
    traffic_delivered_bytes: 18.4 * 1024 ** 4,
    active_servers: 4,
    last_24h: { signups: 34, purchases: 26, revenue_usd: 312.75 },
  },
  charts: {
    revenue_30d: points,
    signups_30d: points.map((p, i) => ({
      ...p,
      value: Math.round(12 + i * 0.7 + Math.sin(i * 0.5) * 8),
    })),
  },
  recent_activity: users.slice(0, 6).map((u, i) => ({
    kind: i % 2 ? "signup" : "order",
    at: new Date(Date.now() - i * 420000).toISOString(),
    amount_usd: 12 + i * 3,
    status: "provisioned",
    user_telegram_id: u.telegram_id,
    user_first_name: u.first_name,
  })),
});
function paginate(items, url) {
  const page = Math.max(1, Number(url.searchParams.get("page") || 1));
  const page_size = Math.max(
    1,
    Number(url.searchParams.get("page_size") || 25),
  );
  return {
    items: items.slice((page - 1) * page_size, page * page_size),
    page,
    page_size,
    total: items.length,
    total_pages: Math.max(1, Math.ceil(items.length / page_size)),
  };
}
function json(res, status, data) {
  res.writeHead(status, {
    "content-type": "application/json;charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(JSON.stringify(data));
}
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://127.0.0.1");
    const route = url.pathname;
    if (route === "/api/dashboard/auth/login" && req.method === "POST") {
      authorized = true;
      return json(res, 200, { ok: true, admin });
    }
    if (route === "/api/dashboard/auth/logout" && req.method === "POST") {
      authorized = false;
      return json(res, 200, { ok: true });
    }
    if (route.startsWith("/api/dashboard/")) {
      if (!authorized)
        return json(res, 401, { detail: "جلسهٔ آزمایشی پایان یافت" });
      if (req.method !== "GET")
        return json(res, 405, {
          detail: "عملیات نوشتن در پیش‌نمایش آزمایشی غیرفعال است",
        });
      if (route.endsWith("/auth/me")) return json(res, 200, admin);
      if (route.endsWith("/overview/operations"))
        return json(res, 200, {
          generated_at: now(),
          pending_receipts: 3,
          pending_payments: 503,
          servers,
        });
      if (route.endsWith("/overview")) return json(res, 200, overview());
      if (route.endsWith("/servers"))
        return json(res, 200, { items: servers, total: servers.length });
      if (route.endsWith("/receipts")) {
        const filter = url.searchParams.get("status");
        const q = (url.searchParams.get("q") || "").toLowerCase();
        const items = (
          filter === "pending" || filter === "all" ? receipts : []
        ).filter((r) =>
          [r.user.first_name, r.user.username, r.user.telegram_id, r.id]
            .join(" ")
            .toLowerCase()
            .includes(q),
        );
        return json(res, 200, paginate(items, url));
      }
      if (route.endsWith("/transactions/pending")) {
        const items = Array.from({ length: 503 }, (_, i) => {
          const r = receipts[i % 3];
          return {
            ...r,
            id: uid(5000 + i),
            user_first_name: r.user.first_name,
            user_telegram_id: r.user.telegram_id,
            provider: "card_to_card",
            kind: "wallet_topup",
            price_amount: r.price_amount_usd,
          };
        });
        return json(res, 200, paginate(items, url));
      }
      if (route.startsWith("/api/dashboard/transactions/"))
        return json(res, 200, {
          items: [],
          total: 0,
          page: 1,
          page_size: 25,
          total_pages: 1,
        });
      if (route === "/api/dashboard/users") {
        const q = (url.searchParams.get("q") || "").toLowerCase();
        const status = url.searchParams.get("status");
        let items = users.filter(
          (u) =>
            [u.first_name, u.username, u.telegram_id]
              .join(" ")
              .toLowerCase()
              .includes(q) &&
            (!status || u.status === status),
        );
        return json(res, 200, {
          items,
          total: items.length,
          page: 1,
          page_size: Number(url.searchParams.get("page_size") || 25),
          total_pages: 1,
        });
      }
      if (route.startsWith("/api/dashboard/users/")) {
        const u = users.find((u) => route.endsWith(u.id));
        if (u)
          return json(res, 200, {
            user: {
              ...u,
              last_name: null,
              personal_discount_percent: 10,
              has_received_free_trial: false,
              is_bot_blocked: false,
              language_code: "fa",
              ref_code: "test",
            },
            subscriptions: [],
            wallet_transactions: [],
          });
      }
      return json(res, 404, { detail: "این بخش دادهٔ آزمایشی ندارد" });
    }
    const requested = path.resolve(dist, "." + decodeURIComponent(route));
    if (!requested.startsWith(dist + path.sep))
      return json(res, 403, { detail: "Forbidden" });
    const file = route.startsWith("/dashboard/assets/")
      ? path.join(dist, "assets", path.basename(route))
      : path.join(dist, "index.html");
    const types = {
      ".js": "text/javascript",
      ".css": "text/css",
      ".html": "text/html",
    };
    const data = await fs.readFile(file);
    res.writeHead(200, {
      "content-type": types[path.extname(file)] || "application/octet-stream",
      "cache-control": "no-store",
    });
    res.end(data);
  } catch {
    json(res, 404, { detail: "Not found" });
  }
});
server.listen(port, "127.0.0.1", () =>
  console.log(`TEST DATA ONLY: http://127.0.0.1:${port}/dashboard/`),
);
