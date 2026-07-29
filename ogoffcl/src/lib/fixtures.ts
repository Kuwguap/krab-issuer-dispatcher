/**
 * Tournament fixture math — shared by the public page and the admin
 * dashboard's simulator.
 *
 * Format: knockout with a FRESH RANDOM DRAW every round (FA Cup style).
 * Each round: shuffle the remaining players, pair them into two-legged ties
 * (two games vs the same opponent, aggregate goals, ET+pens decider if
 * level); winners go into the next round's draw. An odd player count hands
 * one random player a bye into the next round. No prelims, no seeding.
 */
export const LEGS_PER_TIE = 2;

export interface RoundPlan {
  round: number;        // 1-based
  name: string;
  matches: number;      // ties in this round
  byes: number;         // 0 or 1 (odd player count)
  waves: number;        // sequential waves given the concurrency cap
  minutes: number;      // waves * matchMinutes * LEGS_PER_TIE
  breakAfter: number;   // minutes of break inserted after this round (0 = none)
}

export interface SchedulePlan {
  players: number;
  rounds: RoundPlan[];
  totalTies: number;
  totalMinutes: number;             // concurrent play, with breaks
  streamEveryMatchMinutes: number;  // every game sequential on one stream
}

/** Human name for a round based on how many players enter it. */
export function roundNameFor(participants: number, roundNo: number): string {
  if (participants === 2) return "Final";
  if (participants === 4) return "Semi-finals";
  if (participants === 8) return "Quarter-finals";
  return `Round ${roundNo}`;
}

export interface ScheduleOpts {
  concurrent?: number;      // ties running at the same time (default 4)
  matchMinutes?: number;    // real time per GAME incl. setup (default 20)
  breakMinutes?: number;    // standard between-round break (default 10)
  longBreakMinutes?: number;// midpoint break (default 20)
}

export function buildSchedule(players: number, opts: ScheduleOpts = {}): SchedulePlan {
  const n = Math.max(2, Math.floor(players));
  const concurrent = Math.max(1, opts.concurrent ?? 4);
  const matchMinutes = Math.max(5, opts.matchMinutes ?? 20);
  const breakMinutes = opts.breakMinutes ?? 10;
  const longBreakMinutes = opts.longBreakMinutes ?? 20;

  const rounds: RoundPlan[] = [];
  let p = n;
  let r = 1;
  while (p > 1) {
    const ties = Math.floor(p / 2);
    const byes = p % 2;
    rounds.push({ round: r, name: roundNameFor(p, r), matches: ties, byes, waves: 0, minutes: 0, breakAfter: 0 });
    p = ties + byes;
    r += 1;
  }

  const midpoint = Math.floor((rounds.length - 1) / 2);
  let total = 0;
  let streamAll = 0;
  let totalTies = 0;
  rounds.forEach((rd, i) => {
    rd.waves = Math.ceil(rd.matches / concurrent);
    rd.minutes = rd.waves * matchMinutes * LEGS_PER_TIE; // both legs, back to back
    rd.breakAfter = i === rounds.length - 1 ? 0 : i === midpoint ? longBreakMinutes : breakMinutes;
    total += rd.minutes + rd.breakAfter;
    streamAll += rd.matches * matchMinutes * LEGS_PER_TIE + rd.breakAfter;
    totalTies += rd.matches;
  });

  return { players: n, rounds, totalTies, totalMinutes: total, streamEveryMatchMinutes: streamAll };
}

export function fmtDuration(mins: number): string {
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h === 0) return `${m}m`;
  return m ? `${h}h ${m}m` : `${h}h`;
}
