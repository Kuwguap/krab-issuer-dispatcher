import { useEffect, useMemo, useState } from "react";
import { adminApi } from "./AdminLayout";
import { buildSchedule, bracketShape, fmtDuration } from "../lib/fixtures";

/**
 * Tournament HQ: signups + revenue, fixture simulator (any player count),
 * bracket generation / live results, and the canvas studio that renders
 * player match-cards + the ad poster as downloadable PNGs.
 */
interface Player {
  id: string; full_name: string; email: string; phone: string;
  gamertag: string; platform: string; hub: boolean; fee: number;
  payment_status: string; photo: string | null; created_at: string;
}
interface Match {
  id: string; round: number; slot: number;
  player1: string | null; player2: string | null;
  score1: number | null; score2: number | null; winner: string | null;
}

const MIGRATION_HINT = "Run supabase/migration_tournament.sql in the Supabase SQL editor (new tables only — deadlock-safe), then reload.";

// ── canvas studio ──────────────────────────────────────────────────────────
const INK = "#0A0A0A", SMOKE = "#141414", ASH = "#2a2a2a", BONE = "#F5F2EA", ACID = "#C8FF00";

async function loadFonts() {
  // fonts.ready + explicit loads: drawing before Archivo Black is in memory
  // renders fallback glyphs (or half-painted text) into the exported PNG.
  try {
    await document.fonts.ready;
    await Promise.all([
      document.fonts.load('90px "Archivo Black"'),
      document.fonts.load('700 40px "Space Grotesk"'),
    ]);
  } catch {}
}

function grid(ctx: CanvasRenderingContext2D, w: number, h: number) {
  ctx.strokeStyle = "rgba(245,242,234,0.06)";
  ctx.lineWidth = 2;
  for (let x = 0; x < w; x += 72) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
  for (let y = 0; y < h; y += 72) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  ctx.strokeStyle = "rgba(245,242,234,0.08)";
  ctx.beginPath(); ctx.arc(w / 2, h / 2, w * 0.3, 0, Math.PI * 2); ctx.stroke();
}

function slash(ctx: CanvasRenderingContext2D, w: number, y: number, hgt: number) {
  ctx.save();
  ctx.translate(w / 2, y); ctx.rotate(-0.05);
  ctx.fillStyle = "rgba(200,255,0,0.10)";
  ctx.fillRect(-w, -hgt / 2, w * 2, hgt);
  ctx.strokeStyle = "rgba(200,255,0,0.35)"; ctx.lineWidth = 2;
  ctx.strokeRect(-w, -hgt / 2, w * 2, hgt);
  ctx.restore();
}

function loadImg(url: string): Promise<HTMLImageElement | null> {
  return new Promise((res) => {
    const im = new Image();
    im.crossOrigin = "anonymous";
    im.onload = () => res(im);
    im.onerror = () => res(null);
    im.src = url;
  });
}

function coverDraw(ctx: CanvasRenderingContext2D, im: HTMLImageElement, x: number, y: number, w: number, h: number) {
  const s = Math.max(w / im.width, h / im.height);
  const sw = w / s, sh = h / s;
  ctx.drawImage(im, (im.width - sw) / 2, (im.height - sh) * 0.35, sw, sh, x, y, w, h);
}

async function renderPlayerCard(p: Player): Promise<string> {
  await loadFonts();
  const W = 1080, H = 1350;
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = INK; ctx.fillRect(0, 0, W, H);
  grid(ctx, W, H);
  slash(ctx, W, 200, 90);

  // header
  ctx.fillStyle = ACID; ctx.font = '700 26px "Space Grotesk"';
  ctx.textAlign = "left";
  ctx.fillText("O G   O F F C L   P R E S E N T S", 70, 96);
  ctx.fillStyle = BONE; ctx.font = '64px "Archivo Black"';
  ctx.fillText("FC26 KNOCKOUT", 64, 178);

  // photo panel
  const px = 70, py = 260, pw = W - 140, ph = 680;
  ctx.fillStyle = SMOKE; ctx.fillRect(px, py, pw, ph);
  const img = p.photo ? await loadImg(p.photo) : null;
  if (img) {
    ctx.save(); ctx.beginPath(); ctx.rect(px, py, pw, ph); ctx.clip();
    coverDraw(ctx, img, px, py, pw, ph);
    ctx.restore();
    const g = ctx.createLinearGradient(0, py + ph - 240, 0, py + ph);
    g.addColorStop(0, "rgba(10,10,10,0)"); g.addColorStop(1, "rgba(10,10,10,0.92)");
    ctx.fillStyle = g; ctx.fillRect(px, py + ph - 240, pw, 240);
  } else {
    ctx.fillStyle = "rgba(245,242,234,0.12)"; ctx.font = '220px "Archivo Black"'; ctx.textAlign = "center";
    ctx.fillText(p.gamertag.slice(0, 2).toUpperCase(), W / 2, py + ph / 2 + 70);
    ctx.textAlign = "left";
  }
  ctx.strokeStyle = ACID; ctx.lineWidth = 6; ctx.strokeRect(px, py, pw, ph);

  // OVR-style badge
  ctx.fillStyle = ACID; ctx.fillRect(px, py, 150, 150);
  ctx.fillStyle = INK; ctx.textAlign = "center";
  ctx.font = '64px "Archivo Black"'; ctx.fillText("OG", px + 75, py + 84);
  ctx.font = '700 22px "Space Grotesk"'; ctx.fillText("CONTENDER", px + 75, py + 122);

  // platform chip
  ctx.textAlign = "right"; ctx.fillStyle = "rgba(10,10,10,0.85)";
  const plat = p.platform.toUpperCase();
  ctx.fillRect(px + pw - 190, py + 18, 172, 54);
  ctx.fillStyle = ACID; ctx.font = '700 30px "Space Grotesk"';
  ctx.fillText(plat, px + pw - 40, py + 55);

  // gamertag + name
  ctx.textAlign = "left";
  let size = 110;
  ctx.font = `${size}px "Archivo Black"`;
  const tag = p.gamertag.toUpperCase();
  while (ctx.measureText(tag).width > W - 150 && size > 40) { size -= 6; ctx.font = `${size}px "Archivo Black"`; }
  ctx.fillStyle = BONE; ctx.fillText(tag, 70, 1080);
  ctx.fillStyle = ACID; ctx.font = '700 34px "Space Grotesk"';
  ctx.fillText(p.full_name.toUpperCase(), 70, 1140);

  // footer strip
  ctx.fillStyle = ACID; ctx.fillRect(0, H - 110, W, 110);
  ctx.fillStyle = INK; ctx.font = '44px "Archivo Black"'; ctx.textAlign = "left";
  ctx.fillText("OGOFFCL.STORE/TOURNAMENT", 70, H - 40);
  ctx.textAlign = "right"; ctx.font = '700 28px "Space Grotesk"';
  ctx.fillText("GH₵1,000 POT", W - 70, H - 42);
  return c.toDataURL("image/png");
}

async function renderVsGraphic(a: Player, b: Player, roundLabel: string): Promise<string> {
  await loadFonts();
  const W = 1080, H = 1350;
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = INK; ctx.fillRect(0, 0, W, H);
  grid(ctx, W, H);

  ctx.fillStyle = ACID; ctx.font = '700 26px "Space Grotesk"'; ctx.textAlign = "center";
  ctx.fillText("O G   O F F C L   ·   F C 2 6   K N O C K O U T", W / 2, 90);
  ctx.fillStyle = BONE; ctx.font = '58px "Archivo Black"';
  ctx.fillText(roundLabel.toUpperCase(), W / 2, 170);

  const ph = 560, pw = W - 140, px = 70;
  const [ia, ib] = await Promise.all([a.photo ? loadImg(a.photo) : null, b.photo ? loadImg(b.photo) : null]);
  const panel = (img: HTMLImageElement | null, y: number, tagName: string) => {
    ctx.fillStyle = SMOKE; ctx.fillRect(px, y, pw, ph / 2 + 60);
    if (img) {
      ctx.save(); ctx.beginPath(); ctx.rect(px, y, pw, ph / 2 + 60); ctx.clip();
      coverDraw(ctx, img, px, y, pw, ph / 2 + 60);
      const g = ctx.createLinearGradient(0, y, 0, y + ph / 2 + 60);
      g.addColorStop(0.5, "rgba(10,10,10,0)"); g.addColorStop(1, "rgba(10,10,10,0.9)");
      ctx.fillStyle = g; ctx.fillRect(px, y, pw, ph / 2 + 60);
      ctx.restore();
    }
    ctx.strokeStyle = ASH; ctx.lineWidth = 3; ctx.strokeRect(px, y, pw, ph / 2 + 60);
    ctx.fillStyle = BONE; ctx.font = '64px "Archivo Black"'; ctx.textAlign = "left";
    ctx.fillText(tagName.toUpperCase().slice(0, 18), px + 34, y + ph / 2 + 10);
  };
  panel(ia, 240, a.gamertag);
  panel(ib, 700, b.gamertag);

  // VS badge
  ctx.fillStyle = ACID;
  ctx.save(); ctx.translate(W / 2, 620); ctx.rotate(-0.06);
  ctx.fillRect(-120, -70, 240, 140);
  ctx.fillStyle = INK; ctx.font = '96px "Archivo Black"'; ctx.textAlign = "center";
  ctx.fillText("VS", 0, 34);
  ctx.restore();

  ctx.fillStyle = ACID; ctx.fillRect(0, H - 100, W, 100);
  ctx.fillStyle = INK; ctx.font = '40px "Archivo Black"'; ctx.textAlign = "center";
  ctx.fillText("LIVE ON THE OGOFFCL STREAM", W / 2, H - 36);
  return c.toDataURL("image/png");
}

async function renderAdPoster(playerCount: number): Promise<string> {
  await loadFonts();
  const W = 1080, H = 1350;
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = INK; ctx.fillRect(0, 0, W, H);
  grid(ctx, W, H);
  slash(ctx, W, 470, 100);

  // top ribbon
  ctx.fillStyle = ACID; ctx.fillRect(0, 0, W, 78);
  ctx.fillStyle = INK; ctx.font = '700 30px "Space Grotesk"'; ctx.textAlign = "center";
  ctx.fillText("O G   O F F C L   P R E S E N T S", W / 2, 51);

  // headline stack
  ctx.textAlign = "left";
  ctx.fillStyle = BONE; ctx.font = '186px "Archivo Black"';
  ctx.fillText("FC26", 60, 300);
  ctx.strokeStyle = ACID; ctx.lineWidth = 5; ctx.font = '104px "Archivo Black"';
  ctx.strokeText("ULTIMATE TEAM", 62, 420);
  ctx.fillStyle = BONE; ctx.font = '128px "Archivo Black"';
  ctx.fillText("KNOCKOUT", 60, 560);

  // prize banner
  ctx.fillStyle = SMOKE; ctx.fillRect(60, 620, W - 120, 220);
  ctx.strokeStyle = ACID; ctx.lineWidth = 4; ctx.strokeRect(60, 620, W - 120, 220);
  ctx.fillStyle = "rgba(200,255,0,0.6)"; ctx.font = '700 26px "Space Grotesk"';
  ctx.fillText("THE POT — WINNER TAKES ALL", 96, 675);
  ctx.fillStyle = ACID; ctx.font = '84px "Archivo Black"';
  ctx.fillText("GH₵1,000 CASH", 92, 762);
  ctx.fillStyle = BONE; ctx.font = '46px "Archivo Black"';
  ctx.fillText("+ 2 MERCH PIECES FROM THE RACK", 94, 820);

  // chips row
  const chips = ["1V1", "ULTIMATE TEAM", "6-MIN HALVES", "PS5 · XBOX · PC", "STREAMED LIVE"];
  let cx = 60;
  ctx.font = '700 24px "Space Grotesk"';
  for (const t of chips) {
    const w = ctx.measureText(t).width + 44;
    if (cx + w > W - 60) break;
    ctx.strokeStyle = ASH; ctx.lineWidth = 2; ctx.strokeRect(cx, 880, w, 56);
    ctx.fillStyle = "rgba(245,242,234,0.7)";
    ctx.fillText(t, cx + 22, 917);
    cx += w + 14;
  }

  // fee cards
  const card = (x: number, w: number, big: string, small: string) => {
    ctx.fillStyle = SMOKE; ctx.fillRect(x, 980, w, 180);
    ctx.strokeStyle = ACID; ctx.lineWidth = 3; ctx.strokeRect(x, 980, w, 180);
    ctx.fillStyle = ACID; ctx.font = '68px "Archivo Black"'; ctx.textAlign = "left";
    ctx.fillText(big, x + 30, 1070);
    ctx.fillStyle = "rgba(245,242,234,0.6)"; ctx.font = '700 23px "Space Grotesk"';
    ctx.fillText(small, x + 30, 1118);
  };
  card(60, 460, "GH₵50", "ENTRY · SPOT + MATCH CARD");
  card(560, 460, "+GH₵20", "HUB · KNUST KUMASI · 200MBPS");

  ctx.fillStyle = playerCount > 0 ? ACID : "rgba(245,242,234,0.5)";
  ctx.font = '700 30px "Space Grotesk"'; ctx.textAlign = "left";
  ctx.fillText(
    playerCount > 0 ? `${playerCount} PLAYERS ALREADY LOCKED IN — DON'T WATCH FROM THE BENCH` : "LIMITED SPOTS — FIRST PAID, FIRST SEATED",
    60, 1208,
  );

  // footer ribbon
  ctx.fillStyle = ACID; ctx.fillRect(0, H - 100, W, 100);
  ctx.fillStyle = INK; ctx.font = '50px "Archivo Black"'; ctx.textAlign = "center";
  ctx.fillText("OGOFFCL.STORE/TOURNAMENT", W / 2, H - 32);
  return c.toDataURL("image/png");
}

function download(dataUrl: string, name: string) {
  const a = document.createElement("a");
  a.href = dataUrl; a.download = name; a.click();
}

// ── component ──────────────────────────────────────────────────────────────
export default function AdminTournament() {
  const [players, setPlayers] = useState<Player[]>([]);
  const [matches, setMatches] = useState<Match[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [tablesMissing, setTablesMissing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  // studio previews — rendered automatically once players load
  const [posterUrl, setPosterUrl] = useState<string | null>(null);
  const [cardUrls, setCardUrls] = useState<Record<string, string>>({});

  // simulator
  const [simN, setSimN] = useState(20);
  const [simConc, setSimConc] = useState(4);
  const [simLen, setSimLen] = useState(20);
  const [simBreak, setSimBreak] = useState(10);

  const load = async () => {
    const r = await adminApi("/api/tournament", { action: "admin", op: "list" });
    if (r.ok) {
      setPlayers((r.players as Player[]) || []);
      setMatches((r.matches as Match[]) || []);
      setTablesMissing(false);
    } else if (/tables missing|migration_tournament/i.test(String(r.error || ""))) {
      setTablesMissing(true);
    }
  };
  useEffect(() => { load(); }, []);

  // auto-render the studio: poster + one card per signup (photo + name on the card)
  useEffect(() => {
    let alive = true;
    (async () => {
      const paidCount = players.filter((p) => p.payment_status === "paid").length;
      const poster = await renderAdPoster(paidCount).catch(() => null);
      if (alive && poster) setPosterUrl(poster);
      for (const p of players) {
        if (!alive) return;
        if (cardUrls[p.id]) continue;
        const url = await renderPlayerCard(p).catch(() => null);
        if (alive && url) setCardUrls((m) => ({ ...m, [p.id]: url }));
      }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [players]);

  const paid = useMemo(() => players.filter((p) => p.payment_status === "paid"), [players]);
  const revenue = useMemo(() => paid.reduce((a, p) => a + Number(p.fee || 0), 0), [paid]);
  const hubCount = useMemo(() => paid.filter((p) => p.hub).length, [paid]);
  const byId = useMemo(() => Object.fromEntries(players.map((p) => [p.id, p])), [players]);
  const plan = useMemo(() => buildSchedule(simN, { concurrent: simConc, matchMinutes: simLen, breakMinutes: simBreak, longBreakMinutes: simBreak * 2 }), [simN, simConc, simLen, simBreak]);

  const say = (m: string) => { setMsg(m); setTimeout(() => setMsg(null), 3500); };

  const verify = async (p: Player) => {
    if (!confirm(`Mark ${p.gamertag} as PAID (GH₵${p.fee})? Use for cash / direct transfers only.`)) return;
    await adminApi("/api/tournament", { action: "admin", op: "verify", playerId: p.id });
    say(`${p.gamertag} marked paid ✓`); load();
  };
  const remove = async (p: Player) => {
    if (!confirm(`Remove ${p.gamertag} (${p.full_name}) from the tournament?`)) return;
    await adminApi("/api/tournament", { action: "admin", op: "remove", playerId: p.id });
    say("Removed."); load();
  };

  const generateBracket = async () => {
    const n = paid.length;
    if (n < 2) { say("Need at least 2 paid players."); return; }
    if (matches.length && !confirm("Regenerate? The current bracket and all recorded results will be wiped.")) return;
    const shape = bracketShape(n);
    const shuffled = [...paid].sort(() => Math.random() - 0.5);
    const byePlayers = shuffled.slice(0, shape.byes);
    const prelimPlayers = shuffled.slice(shape.byes);
    const rows: { round: number; slot: number; player1: string | null; player2: string | null }[] = [];
    for (let s = 0; s < shape.prelimMatches; s++) {
      rows.push({ round: 0, slot: s, player1: prelimPlayers[2 * s]?.id || null, player2: prelimPlayers[2 * s + 1]?.id || null });
    }
    // main bracket seats: byes first, prelim winners fill the rest later
    const seats: (string | null)[] = Array(shape.bracketSize).fill(null);
    byePlayers.forEach((p, i) => { seats[i] = p.id; });
    const totalRounds = Math.log2(shape.bracketSize);
    for (let r = 1; r <= totalRounds; r++) {
      const count = shape.bracketSize / Math.pow(2, r);
      for (let s = 0; s < count; s++) {
        rows.push({
          round: r, slot: s,
          player1: r === 1 ? seats[2 * s] : null,
          player2: r === 1 ? seats[2 * s + 1] : null,
        });
      }
    }
    setBusy("bracket");
    const r = await adminApi("/api/tournament", { action: "admin", op: "save-matches", matches: rows });
    setBusy(null);
    say(r.ok ? `Bracket generated — ${n} players, ${shape.prelimMatches} prelim matches, ${shape.byes} byes ✓` : `Failed: ${r.error}`);
    load();
  };

  const recordResult = async (m: Match) => {
    const s1 = prompt(`Score for ${byId[m.player1 || ""]?.gamertag || "P1"}:`);
    if (s1 === null) return;
    const s2 = prompt(`Score for ${byId[m.player2 || ""]?.gamertag || "P2"}:`);
    if (s2 === null) return;
    const a = parseInt(s1, 10) || 0, b = parseInt(s2, 10) || 0;
    if (a === b) { say("No draws in a knockout — replay or settle on pens, then enter the deciding score."); return; }
    const winner = a > b ? m.player1 : m.player2;
    if (!winner) { say("Both players must be set first."); return; }
    await adminApi("/api/tournament", { action: "admin", op: "set-result", matchId: m.id, score1: a, score2: b, winner });
    // advance the winner into the next round's seat
    const shape = bracketShape(paid.length);
    let nextRound: number, seat: number;
    if (m.round === 0) { nextRound = 1; seat = shape.byes + m.slot; }
    else { nextRound = m.round + 1; seat = m.slot; }
    const nextSlot = Math.floor(seat / 2);
    const side = seat % 2 === 0 ? "player1" : "player2";
    const next = matches.find((x) => x.round === nextRound && x.slot === nextSlot);
    if (next) await adminApi("/api/tournament", { action: "admin", op: "set-result", matchId: next.id, [side]: winner });
    say("Result saved — winner advanced ✓");
    load();
  };

  const cardFor = async (p: Player) => {
    setBusy(p.id);
    try { download(await renderPlayerCard(p), `og-card-${p.gamertag}.png`); }
    catch { say("Card render failed — photo may not allow cross-origin draw."); }
    setBusy(null);
  };
  const vsFor = async (m: Match) => {
    const a = byId[m.player1 || ""], b = byId[m.player2 || ""];
    if (!a || !b) { say("Both players must be set."); return; }
    setBusy(m.id);
    const label = m.round === 0 ? "Prelims" : matches.filter((x) => x.round === m.round).length === 1 ? "The Final" : `Round ${m.round}`;
    try { download(await renderVsGraphic(a, b, label), `og-vs-${a.gamertag}-${b.gamertag}.png`); }
    catch { say("Render failed."); }
    setBusy(null);
  };

  const roundsGrouped = useMemo(() => {
    const map = new Map<number, Match[]>();
    for (const m of matches) { if (!map.has(m.round)) map.set(m.round, []); map.get(m.round)!.push(m); }
    return [...map.entries()].sort((x, y) => x[0] - y[0]);
  }, [matches]);

  if (tablesMissing) {
    return (
      <div className="border border-ash bg-smoke/50 p-6 max-w-2xl text-sm text-bone/70 space-y-3">
        <p className="text-blood">The tournament tables don't exist yet.</p>
        <p>{MIGRATION_HINT}</p>
      </div>
    );
  }

  return (
    <div className="space-y-12">
      {msg && <div className="fixed top-4 inset-x-4 sm:inset-x-auto sm:right-4 z-50 bg-acid text-ink font-display uppercase text-xs px-4 py-3 shadow-lg">{msg}</div>}

      {/* stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-px bg-ash border border-ash">
        {[
          ["Signups", String(players.length)],
          ["Paid", String(paid.length)],
          ["Hub seats", String(hubCount)],
          ["Revenue", `GH₵${revenue}`],
        ].map(([l, v]) => (
          <div key={l} className="bg-ink p-4 sm:p-5">
            <p className="text-bone/40 text-[10px] uppercase tracking-[0.25em]">{l}</p>
            <p className="font-display text-2xl sm:text-3xl text-acid mt-1">{v}</p>
          </div>
        ))}
      </div>

      {/* studio — live previews, download exactly what you see */}
      <section>
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-4">Card studio</p>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {/* ad poster */}
          <div className="border border-ash bg-smoke/40">
            <div className="aspect-[4/5] bg-ink">
              {posterUrl
                ? <img src={posterUrl} alt="Tournament ad poster" className="w-full h-full object-contain" />
                : <div className="w-full h-full flex items-center justify-center text-bone/30 text-xs animate-pulseSoft">Rendering poster…</div>}
            </div>
            <div className="flex items-center justify-between px-3 py-2.5 border-t border-ash">
              <span className="font-display uppercase text-[10px] tracking-[0.2em] text-acid">Ad poster</span>
              <button disabled={!posterUrl} onClick={() => posterUrl && download(posterUrl, "og-fc26-tournament-ad.png")}
                className="btn-og bg-acid text-ink px-3 py-2 text-[10px] hover:bg-bone">⬇ PNG</button>
            </div>
          </div>
          {/* player cards — rendered automatically from each signup's photo + name */}
          {players.map((p) => (
            <div key={p.id} className="border border-ash bg-smoke/40">
              <div className="aspect-[4/5] bg-ink">
                {cardUrls[p.id]
                  ? <img src={cardUrls[p.id]} alt={`${p.gamertag} card`} className="w-full h-full object-contain" />
                  : <div className="w-full h-full flex items-center justify-center text-bone/30 text-xs animate-pulseSoft">Rendering…</div>}
              </div>
              <div className="flex items-center justify-between gap-2 px-3 py-2.5 border-t border-ash">
                <span className="font-display uppercase text-[10px] tracking-[0.2em] text-bone/70 truncate min-w-0">
                  {p.gamertag}{p.payment_status !== "paid" ? " · unpaid" : ""}
                </span>
                <button disabled={!cardUrls[p.id]} onClick={() => cardUrls[p.id] && download(cardUrls[p.id], `og-card-${p.gamertag}.png`)}
                  className="btn-og bg-bone text-ink px-3 py-2 text-[10px] hover:bg-acid shrink-0">⬇ PNG</button>
              </div>
            </div>
          ))}
        </div>
        <p className="text-bone/35 text-[11px] mt-3">
          Cards render themselves the moment someone signs up — their photo and name land straight on the OG card.
          Downloads are 1080×1350, ready for the feed. VS graphics live on each bracket match below.
        </p>
      </section>

      {/* fixture simulator */}
      <section>
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-4">Fixture simulator</p>
        <div className="flex flex-wrap gap-3 items-end mb-5">
          {[
            ["Players", simN, setSimN, 2, 256],
            ["Concurrent matches", simConc, setSimConc, 1, 32],
            ["Mins / match", simLen, setSimLen, 5, 60],
            ["Break (mins)", simBreak, setSimBreak, 0, 60],
          ].map(([label, val, setter, min, max]) => (
            <label key={label as string} className="block">
              <span className="text-bone/40 text-[10px] uppercase tracking-[0.2em] block mb-1">{label as string}</span>
              <input type="number" min={min as number} max={max as number} value={val as number}
                onChange={(e) => (setter as (n: number) => void)(Math.max(min as number, Math.min(max as number, parseInt(e.target.value, 10) || (min as number))))}
                className="w-28 px-3 py-2.5 text-sm" />
            </label>
          ))}
          <div className="flex gap-2">
            {[8, 16, 20, 32, 40, 64].map((n) => (
              <button key={n} onClick={() => setSimN(n)} className={`btn-og px-3 py-2.5 text-[10px] border-2 ${simN === n ? "bg-acid border-acid text-ink" : "border-ash text-bone/60 hover:border-bone"}`}>{n}</button>
            ))}
          </div>
        </div>

        <div className="border border-ash overflow-x-auto">
          <table className="w-full text-sm min-w-[560px]">
            <thead>
              <tr className="text-bone/40 text-[10px] uppercase tracking-[0.2em] border-b border-ash">
                {["Round", "Matches", "Waves", "Play time", "Break after"].map((h) => <th key={h} className="text-left px-4 py-3 font-normal">{h}</th>)}
              </tr>
            </thead>
            <tbody className="divide-y divide-ash/60">
              {plan.rounds.map((r) => (
                <tr key={r.round}>
                  <td className="px-4 py-2.5 font-display uppercase text-xs text-bone">{r.name}</td>
                  <td className="px-4 py-2.5 text-bone/70">{r.matches}</td>
                  <td className="px-4 py-2.5 text-bone/70">{r.waves}</td>
                  <td className="px-4 py-2.5 text-acid">{fmtDuration(r.minutes)}</td>
                  <td className="px-4 py-2.5 text-bone/40">{r.breakAfter ? `${r.breakAfter}m` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap gap-6 mt-4 text-sm">
          <p className="text-bone/60">Concurrent total: <span className="text-acid font-display">{fmtDuration(plan.totalMinutes)}</span></p>
          <p className="text-bone/60">Every match on one stream: <span className="text-bone font-display">{fmtDuration(plan.streamEveryMatchMinutes)}</span></p>
          <p className="text-bone/40">{plan.prelimMatches > 0 ? `${plan.prelimMatches} prelim matches trim ${plan.players} → ${plan.bracketSize}; ${plan.byes} byes.` : `Clean ${plan.players}-player bracket — no prelims.`}</p>
        </div>
      </section>

      {/* bracket */}
      <section>
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Bracket</p>
          <button onClick={generateBracket} disabled={busy === "bracket"} className="btn-og bg-bone text-ink px-4 py-2.5 text-[10px] hover:bg-acid">
            {busy === "bracket" ? "Saving…" : matches.length ? "Regenerate from paid players" : "Generate from paid players"}
          </button>
          {matches.length > 0 && (
            <button onClick={async () => { if (confirm("Clear the whole bracket?")) { await adminApi("/api/tournament", { action: "admin", op: "clear-matches" }); load(); } }}
              className="btn-og border-2 border-blood/40 text-blood px-4 py-2.5 text-[10px] hover:bg-blood hover:text-bone">
              Clear
            </button>
          )}
          <span className="text-bone/35 text-xs">Bracket is live on /tournament the moment it's generated. Tap a match to record the score — winners advance automatically.</span>
        </div>

        {roundsGrouped.length === 0 ? (
          <p className="border border-ash px-4 py-6 text-sm text-bone/40">No bracket yet — generate one when registrations close.</p>
        ) : (
          <div className="overflow-x-auto pb-2">
            <div className="flex gap-5 min-w-max">
              {roundsGrouped.map(([round, ms]) => (
                <div key={round} className="w-60 shrink-0">
                  <p className="font-display uppercase text-[10px] tracking-[0.25em] text-acid mb-2">
                    {round === 0 ? "Prelims" : ms.length === 1 ? "Final" : ms.length === 2 ? "Semis" : ms.length === 4 ? "Quarters" : `Round of ${ms.length * 2}`}
                  </p>
                  <div className="space-y-2.5">
                    {ms.map((m) => (
                      <div key={m.id} className="border border-ash bg-smoke/40">
                        {[{ id: m.player1, sc: m.score1 }, { id: m.player2, sc: m.score2 }].map((side, si) => (
                          <div key={si} className={`flex items-center gap-2 px-3 py-2 ${si === 0 ? "border-b border-ash/60" : ""} ${m.winner && side.id === m.winner ? "bg-acid/10" : ""}`}>
                            <span className={`flex-1 min-w-0 truncate font-display uppercase text-xs ${m.winner && side.id === m.winner ? "text-acid" : "text-bone/80"}`}>
                              {side.id ? byId[side.id]?.gamertag || "?" : "TBD"}
                            </span>
                            <span className="font-display text-sm text-bone/50">{side.sc ?? "–"}</span>
                          </div>
                        ))}
                        <div className="flex divide-x divide-ash/60 border-t border-ash/60">
                          <button onClick={() => recordResult(m)} className="flex-1 py-2 text-[10px] uppercase tracking-widest text-bone/50 hover:text-acid hover:bg-bone/5">Score</button>
                          <button onClick={() => vsFor(m)} disabled={busy === m.id} className="flex-1 py-2 text-[10px] uppercase tracking-widest text-bone/50 hover:text-acid hover:bg-bone/5">
                            {busy === m.id ? "…" : "VS card"}
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>

      {/* players */}
      <section>
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-4">Signups</p>
        <div className="space-y-2.5">
          {players.map((p) => (
            <div key={p.id} className="flex flex-wrap items-center gap-3 border border-ash bg-smoke/40 p-3">
              <div className="w-12 h-14 bg-smoke border border-ash overflow-hidden shrink-0">
                {p.photo && <img src={p.photo} alt="" loading="lazy" className="w-full h-full object-cover" />}
              </div>
              <div className="flex-1 min-w-[10rem]">
                <p className="font-display uppercase text-sm text-bone">{p.gamertag} <span className="text-bone/30 text-xs normal-case">· {p.full_name}</span></p>
                <p className="text-bone/40 text-xs mt-0.5 break-all">
                  {p.platform.toUpperCase()} · GH₵{Number(p.fee)}{p.hub ? " · HUB" : ""} · {p.email} · {p.phone}
                </p>
              </div>
              <span className={`text-[10px] uppercase tracking-widest px-2 py-1 ${p.payment_status === "paid" ? "bg-acid/15 text-acid" : "bg-blood/15 text-blood"}`}>
                {p.payment_status}
              </span>
              <div className="flex gap-2 ml-auto">
                {p.payment_status !== "paid" && (
                  <button onClick={() => verify(p)} className="btn-og border-2 border-acid/50 text-acid px-3 py-2.5 text-[10px] hover:bg-acid hover:text-ink">Mark paid</button>
                )}
                <button onClick={() => cardFor(p)} disabled={busy === p.id} className="btn-og bg-bone text-ink px-3 py-2.5 text-[10px] hover:bg-acid">
                  {busy === p.id ? "…" : "⬇ Card"}
                </button>
                <button onClick={() => remove(p)} className="btn-og border-2 border-blood/40 text-blood px-3 py-2.5 text-[10px] hover:bg-blood hover:text-bone">Del</button>
              </div>
            </div>
          ))}
          {players.length === 0 && <p className="border border-ash px-4 py-6 text-sm text-bone/40">No signups yet — share ogoffcl.store/tournament.</p>}
        </div>
      </section>
    </div>
  );
}
