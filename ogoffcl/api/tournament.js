// /api/tournament — single function for the whole tournament (keeps the
// Vercel function count down). Dispatch on ?action= / body.action:
//   register        POST  public  — create player row, returns {playerId, fee}
//   pay             POST  public  — Moolre MoMo charge (same OTP/ref-continuity flow as checkout)
//   status          GET   public  — poll payment; marks paid + sends confirmation email
//   bracket         GET   public  — sanitized players + matches for the public page
//   admin           POST  gated   — list / verify / remove / save-matches / set-result / clear-matches
import {
  env, sb, moolrePay, moolrePaymentLink, moolreStatus, sendEmail, emailShell,
} from "./_lib.js";
import { sendPaidEmails, finalizeTournamentByRef } from "./_tournament.js";

const BASE_FEE = 50;
const HUB_FEE = 20;
const SNAP_GROUP = "https://snapchat.com/t/kHlg89aj";

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

// sendPaidEmails / finalizeTournamentByRef live in ./_tournament.js so the
// Moolre webhook (api/pay/callback.js) can finalize + email too.

// ── handlers ───────────────────────────────────────────────────────────────

async function register(req, res) {
  const b = req.body || {};
  const fullName = String(b.fullName || "").trim();
  const email = String(b.email || "").trim().toLowerCase();
  const phone = String(b.phone || "").trim();
  const gamertag = String(b.gamertag || "").trim();
  const platform = ["ps5", "xbox", "pc"].includes(String(b.platform)) ? String(b.platform) : "ps5";
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

  // registering also joins the site mailing list (source: tournament) —
  // disclosed on the form; duplicate emails are ignored, never an error
  sb("POST", "subscribers?on_conflict=email", { email, source: "tournament" },
    { Prefer: "resolution=ignore-duplicates,return=minimal" }).catch(() => {});

  // signup email straight away — spot locks when the payment lands
  sendEmail({
    to: email,
    subject: "You've signed up — OG OFFCL FC26 Tournament ⚽",
    html: emailShell("Signed up. One step left.", `
      <p><strong style="color:#C8FF00;">${gamertag}</strong> — your registration is in. Complete the
      <strong style="color:#C8FF00;">GH₵${fee}</strong> mobile-money payment to lock your spot on the bracket.</p>
      <p style="color:#8b877e;">Kickoff: <strong style="color:#F5F2EA;">1st August</strong>.
      The pot: <strong style="color:#F5F2EA;">GH₵1,000 cash + 2 merch pieces</strong> from the store.
      Matches are FC26 <strong style="color:#F5F2EA;">Ultimate Team</strong> — bring your best squad.</p>
      <p style="color:#C8FF00;">Spaces are limited and only PAID spots count — first paid, first on the bracket.</p>
      <p>You'll get a second email the moment your payment is received.</p>
    `),
  }).catch(() => {});

  return res.json({ playerId: ins.data[0].id, fee });
}

async function pay(req, res) {
  // Preferred: hosted Moolre payment link (Web POS) — Moolre's own page runs
  // the prompt/OTP/approval UX, fixing debit prompts that never arrive.
  // Fallback: the direct charge flow (works today; hosted link activates the
  // moment MOOLRE_API_USER on Vercel matches the Moolre account username —
  // /embed/link verifies it (PL02), the transact endpoints never did).
  const { playerId, phone, channel, otpcode, ref } = req.body || {};
  if (!playerId) return res.status(400).json({ error: "playerId required." });

  const player = await getPlayer(playerId);
  if (!player) return res.status(404).json({ error: "Registration not found." });
  if (player.payment_status === "paid") return res.json({ state: "paid", ref: player.payment_ref });

  const amount = Number(player.fee || BASE_FEE);
  const tag = `TRN-${String(player.id).slice(0, 8).toUpperCase()}`;

  // OTP submits stay on the direct flow — never restart with a link
  if (!otpcode) {
    const attempt = `${tag}-${Date.now().toString(36).slice(-5).toUpperCase()}`;
    const resp = await moolrePaymentLink({
      amount,
      externalref: attempt,
      redirect: `${env.siteUrl}/tournament/pay/${player.id}?ref=${encodeURIComponent(attempt)}`,
      callback: `${env.siteUrl}/api/pay/callback`,
      metadata: { kind: "tournament", playerId: player.id, gamertag: player.gamertag },
    });
    const url = resp && resp.data && resp.data.authorization_url;
    if (Number(resp.status) === 1 && url) {
      await sb("PATCH", `tournament_players?id=eq.${player.id}`, { payment_ref: attempt });
      return res.json({ state: "link", url, ref: attempt });
    }
    // link unavailable (auth/config) — fall through to the direct charge
  }

  const digits = String(phone || "").replace(/\D/g, "");
  if (digits.length < 9) {
    // link creation failed and we have no number for the direct fallback —
    // tell the client to reveal the direct-payment fields
    return res.json({ state: "need-details", error: "Hosted payment is unavailable right now — enter your MoMo number to pay by direct prompt instead." });
  }
  if (!["mtn", "telecel", "at"].includes(String(channel))) return res.status(400).json({ error: "channel must be mtn, telecel or at." });

  // same externalref continuity rule as checkout: resume the exact ref on OTP submit
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
    const done = await finalizeTournamentByRef(ref);
    return res.json({ state: "paid", gamertag: done ? done.gamertag : (player ? player.gamertag : null) });
  }
  if (txstatus === 0 || /fail|declin|cancel/i.test(String(resp.message || ""))) {
    return res.json({ state: "failed", message: resp.message || "Payment failed." });
  }
  return res.json({ state: "pending" });
}

// sanitized single-player lookup for the standalone pay page — the UUID in
// the link is the capability; no email/phone/name is ever returned
async function playerInfo(req, res) {
  const id = String((req.query && req.query.id) || "").trim();
  if (!id) return res.status(400).json({ error: "id required" });
  const p = await getPlayer(id);
  if (!p) return res.status(404).json({ error: "Registration not found." });
  return res.json({
    id: p.id, gamertag: p.gamertag, platform: p.platform, hub: !!p.hub,
    fee: Number(p.fee || BASE_FEE), paymentStatus: p.payment_status,
  });
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
    // manual mark-paid (cash / direct momo) — completes the FULL process:
    // marks paid AND sends the same confirmation emails as an online payment
    const player = await getPlayer(b.playerId);
    if (!player) return res.status(404).json({ error: "player not found" });
    if (player.payment_status === "paid") return res.json({ ok: true, already: true });
    await sb("PATCH", `tournament_players?id=eq.${encodeURIComponent(b.playerId)}`, {
      payment_status: "paid", paid_at: new Date().toISOString(),
    });
    sendPaidEmails(player);
    return res.json({ ok: true, emailed: (player.email || "").includes("@") });
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
  if (act === "linkdiag") {
    // probe /embed/link (admin-gated, no secrets returned): reports whether
    // hosted payment links are active. Optional b.user tries a candidate
    // X-API-USER without touching the environment.
    const headers = {
      "Content-Type": "application/json",
      "X-API-USER": b.user ? String(b.user) : env.moolreUser,
      "X-API-PUBKEY": env.moolrePubKey,
    };
    const r = await fetch("https://api.moolre.com/embed/link", {
      method: "POST",
      headers,
      body: JSON.stringify({
        type: 1, amount: "1.00", email: env.storeNotify || "ogoffcl@gmail.com",
        externalref: `DIAG-${Date.now().toString(36).toUpperCase()}`,
        currency: "GHS", accountnumber: env.moolreAccount, reusable: "0",
      }),
    });
    const j = await r.json().catch(() => ({}));
    return res.json({
      ok: true, triedUser: b.user ? "candidate" : "env",
      linkWorks: Number(j.status) === 1 && !!(j.data && j.data.authorization_url),
      code: j.code, message: j.message,
    });
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
    if (action === "player") return await playerInfo(req, res);
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
