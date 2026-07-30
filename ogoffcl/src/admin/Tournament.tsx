import { useEffect, useMemo, useState } from "react";
import { adminApi } from "./AdminLayout";
import { buildSchedule, roundNameFor, fmtDuration } from "../lib/fixtures";

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
  score1: number | null; score2: number | null;            // leg 1
  leg2_score1: number | null; leg2_score2: number | null;  // leg 2
  pens1: number | null; pens2: number | null;              // shootout (decider)
  winner: string | null;
}

/** Aggregate over both legs, null until any score exists. */
function agg(m: Match, side: 1 | 2): number | null {
  const a = side === 1 ? m.score1 : m.score2;
  const b = side === 1 ? m.leg2_score1 : m.leg2_score2;
  if (a === null && b === null) return null;
  return Number(a || 0) + Number(b || 0);
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
  ctx.fillText("1ST AUGUST", W - 70, H - 42);
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
  const chips = ["AUG 1ST", "1V1", "ULTIMATE TEAM", "PS5 · XBOX · PC", "STREAMED LIVE"];
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

  ctx.fillStyle = ACID;
  ctx.font = '700 30px "Space Grotesk"'; ctx.textAlign = "left";
  ctx.fillText(
    playerCount > 0
      ? `1ST AUGUST · ${playerCount} LOCKED IN · LIMITED SPOTS LEFT`
      : "1ST AUGUST · LIMITED SPOTS — FIRST PAID, FIRST SEATED",
    60, 1208,
  );

  // footer ribbon
  ctx.fillStyle = ACID; ctx.fillRect(0, H - 100, W, 100);
  ctx.fillStyle = INK; ctx.font = '50px "Archivo Black"'; ctx.textAlign = "center";
  ctx.fillText("OGOFFCL.STORE/TOURNAMENT", W / 2, H - 32);
  return c.toDataURL("image/png");
}

/** Mobile-safe download: share sheet where supported (iOS/Android), else a
 *  blob object-URL — `a.download` on a data: URL silently does nothing in
 *  iOS Safari, which is why studio downloads failed on phones. */
/** 1920×1080 OBS scene backgrounds/overlays in the brand style. "frame" and
 *  "bracket" export TRANSPARENT centers — they sit on top of a gameplay feed
 *  or the live fixtures browser source. */
const STREAM_SCREENS = [
  { key: "start", label: "Starting soon" },
  { key: "break", label: "Ad break" },
  { key: "tech", label: "Technical hold" },
  { key: "outro", label: "Champion outro" },
  { key: "frame", label: "Gameplay frame (transparent)" },
  { key: "bracket", label: "Bracket frame (transparent)" },
] as const;
type ScreenKind = (typeof STREAM_SCREENS)[number]["key"];

async function renderStreamScreen(kind: ScreenKind): Promise<string> {
  await loadFonts();
  const W = 1920, H = 1080;
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const ctx = c.getContext("2d")!;
  const transparent = kind === "frame" || kind === "bracket";

  if (!transparent) {
    ctx.fillStyle = INK; ctx.fillRect(0, 0, W, H);
    grid(ctx, W, H);
    slash(ctx, W, H * 0.46, 110);
  }

  const topBar = (text: string) => {
    ctx.fillStyle = ACID; ctx.fillRect(0, 0, W, 86);
    ctx.fillStyle = INK; ctx.font = '40px "Archivo Black"'; ctx.textAlign = "left";
    ctx.fillText(text, 48, 60);
    ctx.font = '700 26px "Space Grotesk"'; ctx.textAlign = "right";
    ctx.fillText("OGOFFCL.STORE/TOURNAMENT", W - 48, 56);
  };
  const bottomBar = (text: string) => {
    ctx.fillStyle = ACID; ctx.fillRect(0, H - 78, W, 78);
    ctx.fillStyle = INK; ctx.font = '34px "Archivo Black"'; ctx.textAlign = "center";
    ctx.fillText(text, W / 2, H - 26);
  };

  if (kind === "start" || kind === "break" || kind === "tech" || kind === "outro") {
    ctx.fillStyle = ACID; ctx.font = '700 30px "Space Grotesk"'; ctx.textAlign = "left";
    ctx.fillText("O G   O F F C L   P R E S E N T S", 96, 150);
    ctx.fillStyle = BONE; ctx.font = '150px "Archivo Black"';
    const heads: Record<string, [string, string]> = {
      start: ["FC26 KNOCKOUT", "STARTING SOON"],
      break: ["QUICK BREAK", "SHOP THE DROP"],
      tech: ["HOLD TIGHT", "FIXING THE FEED"],
      outro: ["WE HAVE A", "CHAMPION"],
    };
    const [l1, l2] = heads[kind];
    ctx.fillText(l1, 90, 330);
    ctx.strokeStyle = ACID; ctx.lineWidth = 5; ctx.font = '150px "Archivo Black"';
    ctx.strokeText(l2, 92, 490);

    ctx.fillStyle = "rgba(245,242,234,0.65)"; ctx.font = '700 36px "Space Grotesk"';
    const subs: Record<string, string> = {
      start: "1ST AUGUST · 1V1 ULTIMATE TEAM · 2-LEG TIES · GH₵1,000 + MERCH POT",
      break: "OGOFFCL.STORE — LIMITED RUNS, NO RESTOCKS · CODE DROPS IN CHAT",
      tech: "THE FOOTBALL CONTINUES — SCORES STILL COUNT · BRACKET IN THE CHAT",
      outro: "GG'S ONLY · GH₵1,000 + 2 MERCH PIECES CLAIMED · NEXT EDITION LOADING",
    };
    ctx.fillText(subs[kind], 96, 580);

    if (kind === "break") {
      // outlined box for the OBS countdown timer to sit inside
      ctx.strokeStyle = ACID; ctx.lineWidth = 4;
      ctx.strokeRect(W - 620, 700, 520, 220);
      ctx.fillStyle = ACID; ctx.font = '700 30px "Space Grotesk"'; ctx.textAlign = "left";
      ctx.fillText("BACK IN", W - 590, 750);
      ctx.fillStyle = "rgba(245,242,234,0.25)"; ctx.font = '24px "Space Grotesk"';
      ctx.fillText("(OBS timer goes here)", W - 590, 890);
    }
    if (kind === "outro") {
      // empty acid panel — screenshot/crop the winner's card or name over it
      ctx.strokeStyle = ACID; ctx.lineWidth = 4;
      ctx.strokeRect(96, 660, 900, 260);
      ctx.fillStyle = "rgba(200,255,0,0.35)"; ctx.font = '700 28px "Space Grotesk"'; ctx.textAlign = "left";
      ctx.fillText("WINNER'S NAME / PLAYER CARD HERE", 130, 800);
    }
    bottomBar("OGOFFCL.STORE/TOURNAMENT — GH₵1,000 + MERCH ON THE LINE");
  }

  if (kind === "frame") {
    // transparent gameplay overlay: border + lower-left live strip + top-right tag
    ctx.strokeStyle = ACID; ctx.lineWidth = 6;
    ctx.strokeRect(8, 8, W - 16, H - 16);
    ctx.fillStyle = ACID; ctx.fillRect(0, H - 64, 760, 64);
    ctx.fillStyle = INK; ctx.font = '32px "Archivo Black"'; ctx.textAlign = "left";
    ctx.fillText("LIVE · OGOFFCL FC26 KNOCKOUT", 24, H - 20);
    ctx.fillStyle = "rgba(10,10,10,0.85)"; ctx.fillRect(W - 320, 0, 320, 56);
    ctx.fillStyle = BONE; ctx.font = '28px "Archivo Black"'; ctx.textAlign = "right";
    ctx.fillText("OG.OFFCL", W - 24, 40);
  }

  if (kind === "bracket") {
    // transparent frame for the live fixtures browser source
    topBar("LIVE FIXTURES — FC26 KNOCKOUT");
    bottomBar("FRESH RANDOM DRAW EVERY ROUND · TWO-LEG TIES · AGGREGATE GOALS");
    ctx.strokeStyle = ACID; ctx.lineWidth = 4;
    ctx.strokeRect(2, 86, W - 4, H - 86 - 78);
  }

  return c.toDataURL("image/png");
}

async function download(dataUrl: string, name: string) {
  try {
    const blob = await (await fetch(dataUrl)).blob();
    const file = new File([blob], name, { type: "image/png" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try { await navigator.share({ files: [file], title: name }); return; }
      catch (ex) { if ((ex as Error).name === "AbortError") return; /* else fall through */ }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch {
    window.open(dataUrl, "_blank"); // last resort: open full-size, long-press to save
  }
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
  const [sampleUrl, setSampleUrl] = useState<string | null>(null);
  const [cardUrls, setCardUrls] = useState<Record<string, string>>({});
  const [screenUrls, setScreenUrls] = useState<Record<string, string>>({});
  const [copied, setCopied] = useState(false);

  // OBS stream screens — player-independent, render once
  useEffect(() => {
    let alive = true;
    (async () => {
      for (const s of STREAM_SCREENS) {
        const url = await renderStreamScreen(s.key).catch(() => null);
        if (!alive) return;
        if (url) setScreenUrls((m) => ({ ...m, [s.key]: url }));
      }
    })();
    return () => { alive = false; };
  }, []);

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
      // template player card — always available for ads even with zero signups
      const sample = await renderPlayerCard({
        id: "sample", full_name: "This could be you", gamertag: "YOUR TAG",
        platform: "ps5", email: "", phone: "", hub: false, fee: 50,
        payment_status: "paid", photo: null, created_at: "",
      } as Player).catch(() => null);
      if (alive && sample) setSampleUrl(sample);
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
    if (!confirm(`Mark ${p.gamertag} as PAID (GH₵${p.fee})? They'll get the confirmation email with the Snapchat group link. Use for cash / direct transfers only.`)) return;
    const r = await adminApi("/api/tournament", { action: "admin", op: "verify", playerId: p.id });
    say(r.ok ? `${p.gamertag} marked paid ✓${r.emailed ? " — confirmation email sent" : ""}` : `Failed: ${r.error}`);
    load();
  };
  const remove = async (p: Player) => {
    if (!confirm(`Remove ${p.gamertag} (${p.full_name}) from the tournament?`)) return;
    await adminApi("/api/tournament", { action: "admin", op: "remove", playerId: p.id });
    say("Removed."); load();
  };

  // ── fresh-draw-per-round knockout ──────────────────────────────
  const roundsGrouped = useMemo(() => {
    const map = new Map<number, Match[]>();
    for (const m of matches) { if (!map.has(m.round)) map.set(m.round, []); map.get(m.round)!.push(m); }
    return [...map.entries()].sort((x, y) => x[0] - y[0]);
  }, [matches]);
  const lastRoundEntry = roundsGrouped.length ? roundsGrouped[roundsGrouped.length - 1] : null;
  const lastRoundDecided = lastRoundEntry ? lastRoundEntry[1].every((m) => m.winner) : false;
  const lastRoundWinners = lastRoundEntry ? lastRoundEntry[1].map((m) => m.winner).filter(Boolean) as string[] : [];
  const champion = lastRoundEntry && lastRoundDecided && lastRoundWinners.length === 1 ? byId[lastRoundWinners[0]] : null;

  const drawRound = async () => {
    let pool: string[];
    let nextRound: number;
    if (!lastRoundEntry) {
      if (paid.length < 2) { say("Need at least 2 paid players."); return; }
      pool = paid.map((p) => p.id);
      nextRound = 1;
    } else {
      if (!lastRoundDecided) { say("Score every tie in the current round first."); return; }
      if (lastRoundWinners.length < 2) { say("The champion is already decided."); return; }
      pool = [...lastRoundWinners];
      nextRound = lastRoundEntry[0] + 1;
    }
    // Fisher–Yates: a genuinely random draw (sort(random) is biased)
    for (let i = pool.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [pool[i], pool[j]] = [pool[j], pool[i]];
    }
    const ties = Math.floor(pool.length / 2);
    const rows: { round: number; slot: number; player1: string | null; player2: string | null; winner?: string }[] = [];
    for (let s = 0; s < ties; s++) {
      rows.push({ round: nextRound, slot: s, player1: pool[2 * s], player2: pool[2 * s + 1] });
    }
    const bye = pool.length % 2 === 1 ? pool[pool.length - 1] : null;
    if (bye) rows.push({ round: nextRound, slot: ties, player1: bye, player2: null, winner: bye });
    setBusy("bracket");
    const r = await adminApi("/api/tournament", { action: "admin", op: "add-matches", matches: rows });
    setBusy(null);
    say(r.ok
      ? `${roundNameFor(pool.length, nextRound)} drawn — ${ties} tie${ties === 1 ? "" : "s"}${bye ? ` + 1 bye (${byId[bye]?.gamertag || "?"})` : ""} ✓`
      : `Failed: ${r.error}`);
    load();
  };

  const redrawRound = async () => {
    if (!lastRoundEntry) return;
    if (!confirm(`Redraw round ${lastRoundEntry[0]}? Its pairings and any scores in it are wiped, then draw again.`)) return;
    await adminApi("/api/tournament", { action: "admin", op: "clear-round", round: lastRoundEntry[0] });
    say(`Round ${lastRoundEntry[0]} cleared — tap Draw to re-shuffle.`);
    load();
  };

  const recordResult = async (m: Match) => {
    // two-legged tie: leg 1 + leg 2 → aggregate → pens if level
    const n1 = byId[m.player1 || ""]?.gamertag || "P1";
    const n2 = byId[m.player2 || ""]?.gamertag || "P2";
    if (!m.player1 || !m.player2) { say("Both players must be set first."); return; }
    const ask = (label: string): number | null => {
      const v = prompt(label);
      if (v === null) return null;
      const n = parseInt(v, 10);
      return Number.isFinite(n) && n >= 0 ? n : 0;
    };
    const l1a = ask(`LEG 1 — goals for ${n1}:`); if (l1a === null) return;
    const l1b = ask(`LEG 1 — goals for ${n2}:`); if (l1b === null) return;
    const l2a = ask(`LEG 2 — goals for ${n1}:`); if (l2a === null) return;
    const l2b = ask(`LEG 2 — goals for ${n2}:`); if (l2b === null) return;
    const aggA = l1a + l2a, aggB = l1b + l2b;
    let pens1: number | null = null, pens2: number | null = null;
    let winner: string;
    if (aggA !== aggB) {
      winner = aggA > aggB ? m.player1 : m.player2;
    } else {
      say(`Level ${aggA}–${aggB} on aggregate — enter the shootout result.`);
      const pa = ask(`PENALTIES — ${n1}:`); if (pa === null) return;
      const pb = ask(`PENALTIES — ${n2}:`); if (pb === null) return;
      if (pa === pb) { say("A shootout can't end level — enter the final scores."); return; }
      pens1 = pa; pens2 = pb;
      winner = pa > pb ? m.player1 : m.player2;
    }
    const r = await adminApi("/api/tournament", {
      action: "admin", op: "set-result", matchId: m.id,
      score1: l1a, score2: l1b, leg2_score1: l2a, leg2_score2: l2b,
      pens1, pens2, winner,
    });
    // no seat-advancement here — winners enter the NEXT round's fresh draw
    say(r.ok ? `Tie saved — ${byId[winner]?.gamertag || "winner"} goes into the next draw ✓` : `Failed: ${r.error}`);
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
          {/* template player card — always downloadable, even before signups */}
          <div className="border border-ash bg-smoke/40">
            <div className="aspect-[4/5] bg-ink">
              {sampleUrl
                ? <img src={sampleUrl} alt="Player card template" className="w-full h-full object-contain" />
                : <div className="w-full h-full flex items-center justify-center text-bone/30 text-xs animate-pulseSoft">Rendering card…</div>}
            </div>
            <div className="flex items-center justify-between px-3 py-2.5 border-t border-ash">
              <span className="font-display uppercase text-[10px] tracking-[0.2em] text-acid">Player card</span>
              <button disabled={!sampleUrl} onClick={() => sampleUrl && download(sampleUrl, "og-player-card.png")}
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

        {/* OBS stream screens */}
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mt-8 mb-4">Stream screens · 1920×1080 for OBS</p>
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {STREAM_SCREENS.map((s) => (
            <div key={s.key} className="border border-ash bg-smoke/40">
              <div className="aspect-video bg-[repeating-conic-gradient(#1a1a1a_0%_25%,#0f0f0f_0%_50%)] bg-[length:24px_24px]">
                {screenUrls[s.key]
                  ? <img src={screenUrls[s.key]} alt={s.label} className="w-full h-full object-contain" />
                  : <div className="w-full h-full flex items-center justify-center text-bone/30 text-xs animate-pulseSoft">Rendering…</div>}
              </div>
              <div className="flex items-center justify-between gap-2 px-3 py-2.5 border-t border-ash">
                <span className="font-display uppercase text-[10px] tracking-[0.2em] text-bone/70 truncate min-w-0">{s.label}</span>
                <button disabled={!screenUrls[s.key]} onClick={() => screenUrls[s.key] && download(screenUrls[s.key], `og-stream-${s.key}.png`)}
                  className="btn-og bg-bone text-ink px-3 py-2 text-[10px] hover:bg-acid shrink-0">⬇ PNG</button>
              </div>
            </div>
          ))}
        </div>
        <p className="text-bone/35 text-[11px] mt-3">
          Backgrounds for your OBS scenes. The two "frame" screens have transparent centers — layer them ON TOP of a
          gameplay feed or the live fixtures browser source. Put an OBS countdown timer inside the Ad-break box.
        </p>
      </section>

      {/* fixture simulator */}
      <section>
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-4">Fixture simulator</p>
        <div className="flex flex-wrap gap-3 items-end mb-5">
          {[
            ["Players", simN, setSimN, 2, 256],
            ["Concurrent matches", simConc, setSimConc, 1, 32],
            ["Mins / game", simLen, setSimLen, 5, 60],
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
          <p className="text-bone/60">Every game on one stream: <span className="text-bone font-display">{fmtDuration(plan.streamEveryMatchMinutes)}</span></p>
          <p className="text-bone/40">Each tie = 2 games (two legs, back to back) — timings include both.</p>
          <p className="text-bone/40">Fresh random draw every round · {plan.totalTies} ties total · odd counts hand one random player a bye.</p>
        </div>
      </section>

      {/* bracket */}
      <section>
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Fixtures</p>
          {!champion && (
            <button onClick={drawRound} disabled={busy === "bracket" || (!!lastRoundEntry && !lastRoundDecided)}
              className="btn-og bg-acid text-ink px-4 py-2.5 text-[10px] hover:bg-bone">
              {busy === "bracket" ? "Drawing…"
                : !lastRoundEntry ? `Draw round 1 (${paid.length} paid players)`
                : lastRoundDecided ? `Draw ${roundNameFor(lastRoundWinners.length, lastRoundEntry[0] + 1).toLowerCase()} (${lastRoundWinners.length} players)`
                : "Score the current round first"}
            </button>
          )}
          {lastRoundEntry && (
            <button
              onClick={async () => {
                const rn = lastRoundEntry[0];
                if (!confirm(`Email every player in round ${rn} their fixture (opponent, match number, fixtures link)? Byes get a bye email.`)) return;
                setBusy("notify");
                const r = await adminApi("/api/tournament", { action: "admin", op: "notify-round", round: rn });
                setBusy(null);
                say(r.ok ? `Fixture emails sent — ${r.sent}/${r.attempted} ✓` : `Failed: ${r.error}`);
              }}
              disabled={busy === "notify"}
              className="btn-og bg-bone text-ink px-4 py-2.5 text-[10px] hover:bg-acid">
              {busy === "notify" ? "Sending…" : `📧 Email round ${lastRoundEntry[0]} fixtures`}
            </button>
          )}
          {lastRoundEntry && (
            <button onClick={redrawRound} className="btn-og border-2 border-ash text-bone/70 px-4 py-2.5 text-[10px] hover:border-bone">
              Redraw round {lastRoundEntry[0]}
            </button>
          )}
          {matches.length > 0 && (
            <button onClick={async () => { if (confirm("Clear ALL rounds and results?")) { await adminApi("/api/tournament", { action: "admin", op: "clear-matches" }); load(); } }}
              className="btn-og border-2 border-blood/40 text-blood px-4 py-2.5 text-[10px] hover:bg-blood hover:text-bone">
              Clear all
            </button>
          )}
          <button
            onClick={async () => { try { await navigator.clipboard.writeText("https://ogoffcl.store/tournament/fixtures"); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch {} }}
            className="btn-og border-2 border-ash text-bone/70 px-4 py-2.5 text-[10px] hover:border-acid hover:text-acid">
            {copied ? "✓ Copied" : "Copy live fixtures link"}
          </button>
          <span className="text-bone/35 text-xs">
            Fresh random draw every round — score all ties, then draw the next round from the winners. Odd count = one random bye. Live at <span className="text-acid">ogoffcl.store/tournament/fixtures</span>.
          </span>
        </div>

        {champion && (
          <div className="border-2 border-acid bg-acid/10 px-5 py-4 mb-4 flex flex-wrap items-center gap-3">
            <span className="font-display uppercase text-[10px] tracking-[0.3em] text-acid">Champion</span>
            <span className="display-xl text-2xl text-bone">{champion.gamertag}</span>
            <span className="text-bone/50 text-xs">GH₵1,000 + 2 merch pieces — settle up 🏆</span>
          </div>
        )}

        {roundsGrouped.length === 0 ? (
          <p className="border border-ash px-4 py-6 text-sm text-bone/40">No fixtures yet — draw round 1 when registrations close.</p>
        ) : (
          <div className="overflow-x-auto pb-2">
            <div className="flex gap-5 min-w-max">
              {roundsGrouped.map(([round, ms]) => (
                <div key={round} className="w-60 shrink-0">
                  <p className="font-display uppercase text-[10px] tracking-[0.25em] text-acid mb-2">
                    {roundNameFor(ms.reduce((a, m) => a + (m.player1 ? 1 : 0) + (m.player2 ? 1 : 0), 0), round)}
                  </p>
                  <div className="space-y-2.5">
                    {ms.map((m) => (
                      <div key={m.id} className="border border-ash bg-smoke/40">
                        {[
                          { id: m.player1, l1: m.score1, l2: m.leg2_score1, pens: m.pens1, aggr: agg(m, 1) },
                          { id: m.player2, l1: m.score2, l2: m.leg2_score2, pens: m.pens2, aggr: agg(m, 2) },
                        ].map((side, si) => {
                          const won = m.winner && side.id === m.winner;
                          return (
                            <div key={si} className={`flex items-center gap-2 px-3 py-2 ${si === 0 ? "border-b border-ash/60" : ""} ${won ? "bg-acid/10" : ""}`}>
                              <span className={`flex-1 min-w-0 truncate font-display uppercase text-xs ${won ? "text-acid" : side.id ? "text-bone/80" : "text-bone/25"}`}>
                                {side.id ? byId[side.id]?.gamertag || "?" : (m.player1 && !m.player2 ? "BYE — sits this round out" : "TBD")}
                              </span>
                              {(side.l1 !== null || side.l2 !== null) && (
                                <span className="text-bone/35 text-[10px]">{side.l1 ?? "–"}·{side.l2 ?? "–"}</span>
                              )}
                              {side.pens !== null && <span className="text-bone/45 text-[10px]">(p{side.pens})</span>}
                              <span className={`font-display text-sm ${won ? "text-acid" : "text-bone/50"}`}>{side.aggr ?? "–"}</span>
                            </div>
                          );
                        })}
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
              <div className="flex flex-wrap gap-2 ml-auto">
                {p.payment_status !== "paid" && (
                  <>
                    <button
                      onClick={async () => {
                        const link = `https://ogoffcl.store/tournament/pay/${p.id}`;
                        try { await navigator.clipboard.writeText(link); say(`Pay link for ${p.gamertag} copied ✓ — send it to them`); }
                        catch { prompt("Copy the pay link:", link); }
                      }}
                      className="btn-og border-2 border-bone/40 text-bone/80 px-3 py-2.5 text-[10px] hover:border-acid hover:text-acid">
                      Pay link
                    </button>
                    <button onClick={() => verify(p)} className="btn-og border-2 border-acid/50 text-acid px-3 py-2.5 text-[10px] hover:bg-acid hover:text-ink">Mark paid</button>
                  </>
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
