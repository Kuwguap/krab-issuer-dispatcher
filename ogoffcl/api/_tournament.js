// Shared tournament finalization — used by /api/tournament (status polling +
// admin mark-paid) AND /api/pay/callback (Moolre webhook), so a player who
// pays on the hosted Moolre page and never returns to the site still gets
// marked paid and emailed. Underscore file: bundled include, not a route.
import { env, sb, sendEmail, emailShell } from "./_lib.js";

const SNAP_GROUP = "https://snapchat.com/t/kHlg89aj";

/** Confirmation emails on a spot locking in. */
function sendPaidEmails(player) {
  if ((player.email || "").includes("@")) {
    sendEmail({
      to: player.email,
      subject: "You're in the OG OFFCL FC26 Tournament ⚽",
      html: emailShell("Spot locked in. 🎮", `
        <p><strong style="color:#C8FF00;">${player.gamertag}</strong> — payment received, your spot in the OG OFFCL FC26 tournament is confirmed.</p>
        <p style="color:#8b877e;">Entry: GH₵${Number(player.fee)}${player.hub ? " (playing from the OGOFFCL Hub at KNUST, Kumasi — console + 200 Mbps connection covered; be there in person on game day)" : ""}.<br/>
        Platform: ${String(player.platform).toUpperCase()}.</p>
        <p>Kickoff: <strong style="color:#C8FF00;">1st August</strong>. The pot: <strong style="color:#C8FF00;">GH₵1,000 cash + 2 merch pieces</strong> from the store.</p>
        <p style="margin:18px 0;"><a href="${SNAP_GROUP}" style="display:inline-block;background:#C8FF00;color:#0A0A0A;font-weight:900;text-decoration:none;padding:14px 22px;text-transform:uppercase;letter-spacing:1px;">👻 Join the Snapchat group</a></p>
        <p style="color:#8b877e;">The Snapchat group is where the bracket drops, kickoff times land and winners report scores — <strong style="color:#F5F2EA;">joining it is not optional</strong>. Link: <a href="${SNAP_GROUP}" style="color:#C8FF00;">${SNAP_GROUP}</a></p>
        <p>On game day: add your opponent via EA ID and play your tie in <strong style="color:#F5F2EA;">Ultimate Team → Friendlies → Play a Friend</strong>. Every tie is <strong style="color:#C8FF00;">TWO games</strong> vs the same opponent — total goals across both decide. Level on aggregate? One decider with <strong style="color:#F5F2EA;">Extra Time &amp; Penalties ON</strong>. Screenshot BOTH full-time screens and drop them in the group.</p>
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

/** Mark the registration behind a payment ref as paid (idempotent) and send
 *  the confirmation emails exactly once. Returns the player row or null. */
async function finalizeTournamentByRef(ref) {
  const { data } = await sb("GET", `tournament_players?payment_ref=eq.${encodeURIComponent(ref)}&select=*&limit=1`);
  const player = Array.isArray(data) && data[0] ? data[0] : null;
  if (!player) return null;
  if (player.payment_status === "paid") return player;
  await sb("PATCH", `tournament_players?id=eq.${player.id}&payment_status=eq.pending`, {
    payment_status: "paid", paid_at: new Date().toISOString(),
  });
  sendPaidEmails(player);
  return { ...player, payment_status: "paid" };
}

export { sendPaidEmails, finalizeTournamentByRef };
