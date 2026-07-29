import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import Marquee from "../components/Marquee";
import Reveal from "../components/Reveal";
import { usePageMeta } from "../lib/seo";

/**
 * Live fixtures — the shareable link for the match group. Polls the bracket
 * every 30s so scores and advancing winners appear without a refresh.
 */
interface BPlayer { id: string; gamertag: string; platform: string; photo: string | null; }
interface BMatch {
  id: string; round: number; slot: number;
  player1: string | null; player2: string | null;
  score1: number | null; score2: number | null;
  leg2_score1?: number | null; leg2_score2?: number | null;
  pens1?: number | null; pens2?: number | null;
  winner: string | null;
}

/** Aggregate across both legs of a tie; null until any score exists. */
function tieAgg(l1: number | null | undefined, l2: number | null | undefined): number | null {
  if ((l1 ?? null) === null && (l2 ?? null) === null) return null;
  return Number(l1 || 0) + Number(l2 || 0);
}

export default function Fixtures() {
  usePageMeta({
    title: "Live fixtures — OG OFFCL FC26 Tournament",
    description: "The live knockout bracket for the OG OFFCL FC26 Ultimate Team tournament. Random draw — scores and winners update in real time.",
    path: "/tournament/fixtures",
  });

  const [players, setPlayers] = useState<BPlayer[]>([]);
  const [matches, setMatches] = useState<BMatch[]>([]);
  const [updated, setUpdated] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const pull = () => {
      fetch("/api/tournament?action=bracket")
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => {
          if (!alive || !j) return;
          setPlayers(j.players || []);
          setMatches(j.matches || []);
          setUpdated(new Date().toLocaleTimeString());
        })
        .catch(() => {});
    };
    pull();
    const id = window.setInterval(pull, 30_000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);

  const byId = useMemo(() => Object.fromEntries(players.map((p) => [p.id, p])), [players]);
  const rounds = useMemo(() => {
    const map = new Map<number, BMatch[]>();
    for (const m of matches) { if (!map.has(m.round)) map.set(m.round, []); map.get(m.round)!.push(m); }
    return [...map.entries()].sort((a, b) => a[0] - b[0]);
  }, [matches]);

  const champion = useMemo(() => {
    if (!rounds.length) return null;
    const [, finals] = rounds[rounds.length - 1];
    return finals.length === 1 && finals[0].winner ? byId[finals[0].winner] : null;
  }, [rounds, byId]);

  return (
    <div>
      <div className="max-w-7xl mx-auto px-4 sm:px-6 pt-12 pb-6">
        <Reveal>
          <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">
            <span className="inline-block w-2 h-2 bg-blood rounded-full animate-pulseSoft mr-2 align-middle" />
            Live · random draw · updates automatically
          </p>
          <h1 className="display-xl text-5xl sm:text-7xl text-bone">
            The <span className="text-stroke-acid">fixtures</span>
          </h1>
          <p className="text-bone/50 text-sm uppercase tracking-[0.25em] mt-5">
            FC26 Ultimate Team knockout · two-legged ties · 1st August · {players.length} players{updated ? ` · updated ${updated}` : ""}
          </p>
        </Reveal>
      </div>

      <Marquee text="GH₵1,000 + 2 MERCH ON THE LINE — LIVE BRACKET — OG OFFCL FC26 — " className="bg-smoke text-bone/60" />

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-10">
        {champion && (
          <div className="border-2 border-acid bg-acid/10 p-6 sm:p-8 mb-10 text-center">
            <p className="font-display uppercase text-[11px] tracking-[0.4em] text-acid mb-2">Champion</p>
            <p className="display-xl text-4xl sm:text-6xl text-bone">{champion.gamertag}</p>
            <p className="text-bone/50 text-xs uppercase tracking-[0.25em] mt-3">GH₵1,000 + 2 merch pieces — claimed.</p>
          </div>
        )}

        {rounds.length === 0 ? (
          <div className="text-center py-20">
            <p className="display-xl text-5xl text-bone/15">Draw pending</p>
            <p className="text-bone/40 text-xs uppercase tracking-[0.3em] mt-4 leading-relaxed">
              The random draw drops here once registration closes.<br />Check back — this page updates itself.
            </p>
            <Link to="/tournament" className="btn-og bg-acid text-ink px-8 py-4 text-sm mt-8 inline-flex hover:bg-bone">
              Register — GH₵50 →
            </Link>
          </div>
        ) : (
          <div className="overflow-x-auto pb-4 -mx-4 px-4">
            <div className="flex gap-6 min-w-max">
              {rounds.map(([round, ms]) => (
                <div key={round} className="w-60 shrink-0">
                  <p className="font-display uppercase text-[10px] tracking-[0.25em] text-acid mb-3">
                    {round === 0 ? "Prelims" : ms.length === 1 ? "Final" : ms.length === 2 ? "Semi-finals" : ms.length === 4 ? "Quarter-finals" : `Round of ${ms.length * 2}`}
                  </p>
                  <div className="space-y-3">
                    {ms.map((m) => (
                      <div key={m.id} className="border border-ash bg-ink">
                        {[
                          { id: m.player1, l1: m.score1, l2: m.leg2_score1, pens: m.pens1, aggr: tieAgg(m.score1, m.leg2_score1) },
                          { id: m.player2, l1: m.score2, l2: m.leg2_score2, pens: m.pens2, aggr: tieAgg(m.score2, m.leg2_score2) },
                        ].map((side, si) => {
                          const pl = side.id ? byId[side.id] : null;
                          const won = m.winner && side.id === m.winner;
                          return (
                            <div key={si} className={`flex items-center gap-2.5 px-3 py-2.5 ${si === 0 ? "border-b border-ash/60" : ""} ${won ? "bg-acid/10" : ""}`}>
                              <div className="w-7 h-8 bg-smoke overflow-hidden shrink-0">
                                {pl?.photo && <img src={pl.photo} alt="" loading="lazy" className="w-full h-full object-cover" />}
                              </div>
                              <span className={`flex-1 min-w-0 truncate font-display uppercase text-xs ${won ? "text-acid" : "text-bone/80"}`}>
                                {pl ? pl.gamertag : "TBD"}
                              </span>
                              {side.aggr !== null && (
                                <span className="text-bone/35 text-[10px]">{side.l1 ?? "–"}·{side.l2 ?? "–"}</span>
                              )}
                              {(side.pens ?? null) !== null && <span className="text-bone/45 text-[10px]">(p{side.pens})</span>}
                              <span className={`font-display text-sm ${won ? "text-acid" : "text-bone/40"}`}>{side.aggr ?? "–"}</span>
                            </div>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="mt-12 flex flex-wrap items-center gap-4">
          <Link to="/tournament" className="btn-og border-2 border-bone text-bone px-6 py-3.5 text-xs hover:bg-bone hover:text-ink">
            ← Tournament home
          </Link>
          <p className="text-bone/30 text-[11px] uppercase tracking-[0.25em]">Winner reports the full-time screenshot in the match group</p>
        </div>
      </div>
    </div>
  );
}
