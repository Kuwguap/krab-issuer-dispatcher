// /api/tournament — single function for the whole tournament (keeps the
// Vercel function count down). Dispatch on ?action= / body.action:
//   register        POST  public  — create player row, returns {playerId, fee}
//   pay             POST  public  — Moolre MoMo charge (same OTP/ref-continuity flow as checkout)
//   status          GET   public  — poll payment; marks paid + sends confirmation email
//   bracket         GET   public  — sanitized players + matches for the public page
//   admin           POST  gated   — list / verify / remove / save-matches / set-result / clear-matches
import {
  env, sb, moolrePay, moolreStatus, sendEmail, emailShell,
} from "./_lib.js";

const BASE_FEE = 50;
const HUB_FEE = 20;

const publicPhoto = (p) =>
  p ? `${env.supabaseUrl}/storage/v1/object/public/images/${String(p).replace(/^\/+/, "")}` : null;

async function getPlayer(id) {
  const { data } = await sb("GET", `tournament_players?id=eq.${encodeURIComponent(id)}&select=*&limit=1`);
  return Array.isArray(data) && data[0] ? data[0] : null;
}

async function playerForRef(ref) {
  const { data } = await sb("GET", `tournament_players?payment_ref=eq.${encodeURIComponent(ref)}&select=*&limit=1`);
  return Array.isArray(data) && data[0] ? data[0] : null;
}

// ── handlers ───────────────────────────────────────────────────────────────

async function register(req, res) {
  const b = req.body || {};
  const fullName = String(b.fullName || "").trim();
  const email = String(b.email || "").trim().toLowerCase();
  const phone = String(b.phone || "").trim();
  const gamertag = String(b.gamertag || "").trim();
  const platform = ["ps5", "ps4", "xbox", "pc"].includes(String(b.platform)) ? String(b.platform) : "ps5";
  const hub = !!b.hub;
  const photoPath = String(b.photoPath || "").trim() || null;

  if (!fullName || !email.includes("@") || phone.replace(/\D/g, "").length < 9 || !gamertag) {
    return res.status(400).json({ error: "Name, valid email, phone and your PSN / EA ID are required." });
  }
  if (photoPath && !/^tournament\//.test(photoPath)) {
    return res.status(400).json({ error: "Bad photo path." });
  }

  // one paid spot per gamertag; allow retry if a previous attempt never paid
  const dupe = await sb("GET", `tournament_players?gamertag=ilike.${encodeURIComponent(gamertag)}&payment_status=eq.paid&select=id&limit=1`);
  if (dupe.ok && Array.isArray(dupe.data) && dupe.data[0]) {
    return res.status(409).json({ error: "That gamertag is already registered and paid." });
  }

  const fee = BASE_FEE + (hub ? HUB_FEE : 0);
  const ins = await sb("POST", "tournament_players", {
    full_name: fullName, email, phone, gamertag, platform, hub, fee,
    photo_path: photoPath, payment_status: "pending",
  }, { Prefer: "return=representation" });
  if (!ins.ok || !Array.isArray(ins.data) || !ins.data[0]) {
    const detail = JSON.stringify(ins.data || {}).slice(0, 160);
    return res.status(500).json({ error: "Could not save your registration.", detail });
  }

  // signup email straight away — spot locks when the payment lands
  sendEmail({
    to: email,
    subject: "You've signed up — OG OFFCL FC26 Tournament ⚽",
    html: emailShell("Signed up. One step left.", `
      <p><strong style="color:#C8FF00;">${gamertag}</strong> — your registration is in. Complete the
      <strong style="color:#C8FF00;">GH₵${fee}</strong> mobile-money payment to lock your spot on the bracket.</p>
      <p style="color:#8b877e;">The pot: <strong style="color:#F5F2EA;">GH₵1,000 cash + 2 merch pieces</strong> from the store.
      Matches are FC26 <strong style="color:#F5F2EA;">Ultimate Team</strong> — bring your best squad.</p>
      <p>You'll get a second email the moment your payment is received.</p>
    `),
  }).catch(() => {});

  return res.json({ playerId: ins.data[0].id, fee });
}

async function pay(req, res) {
  const { playerId, phone, channel, otpcode, ref } = req.body || {};
  const digits = String(phone || "").replace(/\D/g, "");
  if (!playerId || digits.length < 9) return res.status(400).json({ error: "playerId and a valid MoMo number are required." });
  if (!["mtn", "telecel", "at"].includes(String(channel))) return res.status(400).json({ error: "channel must be mtn, telecel or at." });

  const player = await getPlayer(playerId);
  if (!player) return res.status(404).json({ error: "Registration not found." });
  if (player.payment_status === "paid") return res.json({ state: "paid", ref: player.payment_ref });

  const amount = Number(player.fee || BASE_FEE);
  // same externalref continuity rule as checkout: resume the exact ref on OTP submit
  const tag = `TRN-${String(player.id).slice(0, 8).toUpperCase()}`;
  const clientRef = typeof ref === "string" && ref.startsWith(`${tag}-`) ? ref : null;
  const resumeRef = otpcode ? (clientRef || player.payment_ref || null) : null;
  const attempt = resumeRef || `${tag}-${Date.now().toString(36).slice(-5).toUpperCase()}`;

  await sb("PATCH", `tournament_players?id=eq.${player.id}`, {
    momo_phone: digits, momo_channel: channel, payment_ref: attempt,
  });

  const resp = await moolrePay({
    channel, phone: digits, amount, externalref: attempt,
    reference: `OG OFFCL Tournament ${player.gamertag}`.slice(0, 40),
    otpcode,
  });

  if (resp.code === "TP14") {
    return res.json({ state: "otp", ref: attempt, message: resp.message || "Enter the OTP sent to you by SMS." });
  }
  if (Number(resp.status) === 1) {
    return res.json({ state: "pending", ref: attempt });
  }
  return res.status(400).json({
    state: "failed", ref: attempt,
    error: resp.message || `Payment could not be started (${resp.code || "unknown"}).`,
  });
}

async function status(req, res) {
  const ref = String((req.query && req.query.ref) || "").trim();
  if (!ref) return res.status(400).json({ error: "ref required" });

  const player = await playerForRef(ref);
  if (player && player.payment_status === "paid") return res.json({ state: "paid", gamertag: player.gamertag });

  const resp = await moolreStatus(ref);
  const tx = resp && typeof resp.data === "object" ? resp.data : null;
  const txstatus = tx ? Number(tx.txstatus) : null;

  if (txstatus === 1) {
    if (player) {
      await sb("PATCH", `tournament_players?id=eq.${player.id}`, {
        payment_status: "paid", paid_at: new Date().toISOString(),
      });
      if ((player.email || "").includes("@")) {
        sendEmail({
          to: player.email,
          subject: "You're in the OG OFFCL FC26 Tournament ⚽",
          html: emailShell("Spot locked in. 🎮", `
            <p><strong style="color:#C8FF00;">${player.gamertag}</strong> — payment received, your spot in the OG OFFCL FC26 tournament is confirmed.</p>
            <p style="color:#8b877e;">Entry: GH₵${Number(player.fee)}${player.hub ? " (playing from the OGOFFCL Hub at KNUST, Kumasi — console + 200 Mbps connection covered; be there in person on game day)" : ""}.<br/>
            Platform: ${String(player.platform).toUpperCase()}.</p>
            <p>The pot: <strong style="color:#C8FF00;">GH₵1,000 cash + 2 merch pieces</strong> from the store.</p>
            <p>What happens next: we'll email + WhatsApp the bracket, your kickoff time and the official match group before game day. Add your opponent via EA ID, invite them from <strong style="color:#F5F2EA;">Ultimate Team → Friendlies → Play a Friend</strong>, screenshot the final score.</p>
            <p style="color:#8b877e;">Bring your best squad. Limited spots — no restocks on glory.</p>
          `),
        }).catch(() => {});
      }
      if (env.storeNotify) {
        sendEmail({
          to: env.storeNotify,
          subject: `🎮 TOURNAMENT ENTRY — ${player.gamertag} paid GH₵${Number(player.fee)}`,
          html: emailShell("New tournament entry", `
            <p><strong>${player.full_name}</strong> (${player.gamertag}, ${String(player.platform).toUpperCase()})${player.hub ? " · HUB PLAYER" : ""}</p>
            <p style="color:#8b877e;">${player.email} · ${player.phone}</p>
          `),
        }).catch(() => {});
      }
    }
    return res.json({ state: "paid", gamertag: player ? player.gamertag : null });
  }
  if (txstatus === 0 || /fail|declin|cancel/i.test(String(resp.message || ""))) {
    return res.json({ state: "failed", message: resp.message || "Payment failed." });
  }
  return res.json({ state: "pending" });
}

async function bracket(_req, res) {
  const [pRes, mRes] = await Promise.all([
    sb("GET", "tournament_players?payment_status=eq.paid&select=id,gamertag,platform,photo_path,seed&order=created_at.asc"),
    sb("GET", "tournament_matches?select=*&order=round.asc,slot.asc"),
  ]);
  const players = (Array.isArray(pRes.data) ? pRes.data : []).map((p) => ({
    id: p.id, gamertag: p.gamertag, platform: p.platform, seed: p.seed,
    photo: publicPhoto(p.photo_path),
  }));
  const matches = Array.isArray(mRes.data) ? mRes.data : [];
  return res.json({ players, matches, count: players.length });
}

async function admin(req, res) {
  if (String(req.headers["x-admin-password"] || "") !== env.adminPassword) {
    return res.status(401).json({ error: "unauthorized" });
  }
  const b = req.body || {};
  const act = String(b.op || "list");

  if (act === "list") {
    const [pRes, mRes] = await Promise.all([
      sb("GET", "tournament_players?select=*&order=created_at.desc"),
      sb("GET", "tournament_matches?select=*&order=round.asc,slot.asc"),
    ]);
    const players = (Array.isArray(pRes.data) ? pRes.data : []).map((p) => ({ ...p, photo: publicPhoto(p.photo_path) }));
    return res.json({ ok: true, players, matches: Array.isArray(mRes.data) ? mRes.data : [] });
  }
  if (act === "verify") {
    // manual mark-paid (cash / direct momo)
    await sb("PATCH", `tournament_players?id=eq.${encodeURIComponent(b.playerId)}`, {
      payment_status: "paid", paid_at: new Date().toISOString(),
    });
    return res.json({ ok: true });
  }
  if (act === "remove") {
    await sb("DELETE", `tournament_players?id=eq.${encodeURIComponent(b.playerId)}`);
    return res.json({ ok: true });
  }
  if (act === "save-matches") {
    // replace the whole bracket: [{round, slot, player1, player2}]
    if (!Array.isArray(b.matches)) return res.status(400).json({ error: "matches array required" });
    await sb("DELETE", "tournament_matches?round=gte.0");
    if (b.matches.length) {
      const rows = b.matches.map((m) => ({
        round: Number(m.round), slot: Number(m.slot),
        player1: m.player1 || null, player2: m.player2 || null,
      }));
      const ins = await sb("POST", "tournament_matches", rows, { Prefer: "return=minimal" });
      if (!ins.ok) return res.status(500).json({ error: "Could not save bracket", detail: JSON.stringify(ins.data).slice(0, 160) });
    }
    return res.json({ ok: true });
  }
  if (act === "set-result") {
    const { matchId, score1, score2, winner, player1, player2 } = b;
    const patch = {};
    if (score1 !== undefined) patch.score1 = score1 === null ? null : Number(score1);
    if (score2 !== undefined) patch.score2 = score2 === null ? null : Number(score2);
    if (winner !== undefined) patch.winner = winner || null;
    if (player1 !== undefined) patch.player1 = player1 || null;
    if (player2 !== undefined) patch.player2 = player2 || null;
    await sb("PATCH", `tournament_matches?id=eq.${encodeURIComponent(matchId)}`, patch);
    return res.json({ ok: true });
  }
  if (act === "clear-matches") {
    await sb("DELETE", "tournament_matches?round=gte.0");
    return res.json({ ok: true });
  }
  return res.status(400).json({ error: `unknown op ${act}` });
}

// ── router ─────────────────────────────────────────────────────────────────
export default async (req, res) => {
  try {
    const action = String((req.query && req.query.action) || (req.body && req.body.action) || "").trim();
    if (action === "register" && req.method === "POST") return await register(req, res);
    if (action === "pay" && req.method === "POST") return await pay(req, res);
    if (action === "status") return await status(req, res);
    if (action === "bracket") return await bracket(req, res);
    if (action === "admin" && req.method === "POST") return await admin(req, res);
    return res.status(400).json({ error: "unknown action" });
  } catch (e) {
    const msg = e && e.message ? e.message : "tournament api failed";
    if (/tournament_players|tournament_matches|PGRST205/.test(msg)) {
      return res.status(503).json({ error: "Tournament tables missing — run supabase/migration_tournament.sql." });
    }
    return res.status(500).json({ error: msg });
  }
};
