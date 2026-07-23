// GET /sitemap.xml (rewritten here via vercel.json)
// Dynamic sitemap: static pages + every active product, fresh from the DB.
import { sb } from "./_lib.js";

const SITE = "https://ogoffcl.store";

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export default async (_req, res) => {
  const urls = [
    { loc: `${SITE}/`, priority: "1.0", changefreq: "weekly" },
    { loc: `${SITE}/shop`, priority: "0.9", changefreq: "daily" },
    { loc: `${SITE}/gallery`, priority: "0.7", changefreq: "weekly" },
    { loc: `${SITE}/tournament`, priority: "0.9", changefreq: "daily" },
    { loc: `${SITE}/tournament/fixtures`, priority: "0.8", changefreq: "hourly" },
    { loc: `${SITE}/refund-policy`, priority: "0.3", changefreq: "yearly" },
  ];

  try {
    const { ok, data } = await sb(
      "GET",
      "products?is_active=eq.true&select=id,updated_at,created_at&order=created_at.desc&limit=500",
    );
    if (ok && Array.isArray(data)) {
      for (const p of data) {
        urls.push({
          loc: `${SITE}/product/${p.id}`,
          priority: "0.8",
          changefreq: "weekly",
          lastmod: (p.updated_at || p.created_at || "").slice(0, 10) || undefined,
        });
      }
    }
  } catch { /* static entries still ship */ }

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls
  .map(
    (u) => `  <url>
    <loc>${esc(u.loc)}</loc>${u.lastmod ? `\n    <lastmod>${u.lastmod}</lastmod>` : ""}
    <changefreq>${u.changefreq}</changefreq>
    <priority>${u.priority}</priority>
  </url>`,
  )
  .join("\n")}
</urlset>`;

  res.setHeader("Content-Type", "application/xml; charset=utf-8");
  res.setHeader("Cache-Control", "s-maxage=3600, stale-while-revalidate=86400");
  return res.status(200).send(xml);
};
