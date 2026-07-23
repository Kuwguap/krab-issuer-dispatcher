import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { supabase } from "../lib/supabase";
import type { Category, Product } from "../lib/types";
import ProductCard from "../components/ProductCard";
import Reveal from "../components/Reveal";

export default function Shop() {
  const [params, setParams] = useSearchParams();
  const activeSlug = params.get("c") || "";
  const [products, setProducts] = useState<Product[]>([]);
  const [cats, setCats] = useState<Category[]>([]);
  const [loading, setLoading] = useState(true);
  const [sort, setSort] = useState<"new" | "low" | "high">("new");

  useEffect(() => {
    supabase.from("categories").select("*").order("name").then(({ data }) => setCats((data || []) as Category[]));
  }, []);

  useEffect(() => {
    setLoading(true);
    supabase
      .from("products")
      .select("*")
      .eq("is_active", true)
      .order("created_at", { ascending: false })
      .then(({ data }) => {
        setProducts((data || []) as Product[]);
        setLoading(false);
      });
  }, []);

  const filtered = useMemo(() => {
    const cat = cats.find((c) => c.slug === activeSlug);
    let list = products;
    if (cat) list = list.filter((p) => p.category_id === cat.id);
    if (sort === "low") list = [...list].sort((a, b) => Number(a.price) - Number(b.price));
    if (sort === "high") list = [...list].sort((a, b) => Number(b.price) - Number(a.price));
    return list;
  }, [products, cats, activeSlug, sort]);

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 py-12">
      <Reveal>
        <h1 className="display-xl text-6xl sm:text-8xl text-bone mb-3">
          Shop<span className="text-acid">.</span>
        </h1>
        <p className="text-bone/50 text-sm uppercase tracking-[0.25em] mb-10">
          {loading ? "Loading the rack…" : `${filtered.length} piece${filtered.length === 1 ? "" : "s"} on the rack`}
        </p>
      </Reveal>

      {/* filters */}
      <div className="flex flex-wrap items-center gap-2 mb-10 sticky top-16 z-30 bg-ink/90 backdrop-blur-md py-3 -mx-4 px-4 sm:-mx-6 sm:px-6 border-b border-ash">
        <button
          onClick={() => setParams({}, { replace: true })}
          className={`btn-og px-4 py-3 sm:py-2 text-xs border-2 ${!activeSlug ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`}
        >
          All
        </button>
        {cats.map((c) => (
          <button
            key={c.id}
            onClick={() => setParams({ c: c.slug }, { replace: true })}
            className={`btn-og px-4 py-3 sm:py-2 text-xs border-2 ${activeSlug === c.slug ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`}
          >
            {c.name}
          </button>
        ))}
        <div className="ml-auto">
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as typeof sort)}
            className="px-3 py-3 sm:py-2 text-xs font-display uppercase tracking-wider"
          >
            <option value="new">Newest</option>
            <option value="low">Price ↑</option>
            <option value="high">Price ↓</option>
          </select>
        </div>
      </div>

      {loading ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
          {[...Array(8)].map((_, i) => (
            <div key={i} className="aspect-[4/5] bg-smoke border border-ash animate-pulseSoft" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="py-32 text-center">
          <p className="display-xl text-5xl text-bone/15">Nothing here</p>
          <p className="text-bone/50 text-sm uppercase tracking-widest mt-4">This rack is empty — check the other lines.</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
          {filtered.map((p, i) => (
            <ProductCard key={p.id} p={p} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}
