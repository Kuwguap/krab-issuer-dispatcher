import { useEffect } from "react";

/**
 * Lightweight SPA SEO: per-route <title>, meta description, canonical,
 * Open Graph/Twitter tags and JSON-LD — no react-helmet dependency.
 * Google renders JS, so runtime tags are picked up for indexing; the
 * static defaults in index.html cover non-rendering scrapers.
 */

export const SITE_URL = "https://ogoffcl.store";
export const SITE_NAME = "OG OFFCL — Original Gangster Official";
export const DEFAULT_OG_IMAGE =
  "https://ztleknsyyxgtbiebjudq.supabase.co/storage/v1/object/public/images/products/1781549732390-lypqi0c2bl.jpeg";

function upsertMeta(attr: "name" | "property", key: string, content: string) {
  let el = document.head.querySelector<HTMLMetaElement>(`meta[${attr}="${key}"]`);
  if (!el) {
    el = document.createElement("meta");
    el.setAttribute(attr, key);
    document.head.appendChild(el);
  }
  el.setAttribute("content", content);
}

function upsertLink(rel: string, href: string) {
  let el = document.head.querySelector<HTMLLinkElement>(`link[rel="${rel}"]`);
  if (!el) {
    el = document.createElement("link");
    el.setAttribute("rel", rel);
    document.head.appendChild(el);
  }
  el.setAttribute("href", href);
}

export interface PageMeta {
  title: string;
  description: string;
  /** path only, e.g. "/gallery" — canonical + og:url are built from it */
  path?: string;
  image?: string;
  /** checkout / confirmation / admin — keep out of the index */
  noindex?: boolean;
  type?: "website" | "product" | "article";
}

export function usePageMeta(meta: PageMeta) {
  useEffect(() => {
    const url = `${SITE_URL}${meta.path ?? window.location.pathname}`;
    document.title = meta.title;
    upsertMeta("name", "description", meta.description);
    upsertMeta("name", "robots", meta.noindex ? "noindex,nofollow" : "index,follow");
    upsertMeta("property", "og:title", meta.title);
    upsertMeta("property", "og:description", meta.description);
    upsertMeta("property", "og:url", url);
    upsertMeta("property", "og:type", meta.type || "website");
    upsertMeta("property", "og:image", meta.image || DEFAULT_OG_IMAGE);
    upsertMeta("name", "twitter:card", "summary_large_image");
    upsertMeta("name", "twitter:title", meta.title);
    upsertMeta("name", "twitter:description", meta.description);
    upsertMeta("name", "twitter:image", meta.image || DEFAULT_OG_IMAGE);
    upsertLink("canonical", url);
  }, [meta.title, meta.description, meta.path, meta.image, meta.noindex, meta.type]);
}

/** Inject (or replace) a JSON-LD structured-data block keyed by id. */
export function useJsonLd(id: string, data: object | null) {
  useEffect(() => {
    const dom = `seo-jsonld-${id}`;
    document.getElementById(dom)?.remove();
    if (!data) return;
    const s = document.createElement("script");
    s.type = "application/ld+json";
    s.id = dom;
    s.text = JSON.stringify(data);
    document.head.appendChild(s);
    return () => { document.getElementById(dom)?.remove(); };
  }, [id, JSON.stringify(data)]);
}
