/**
 * Tournament fixture math — shared by the public page and the admin
 * dashboard's simulator. Single-elimination with a preliminary round that
 * trims any player count down to a clean power-of-two bracket.
 */

export interface RoundPlan {
  round: number;        // 0 = prelims, then 1..k
  name: string;
  matches: number;
  waves: number;        // how many sequential waves given the concurrency cap
  minutes: number;      // waves * matchMinutes
  breakAfter: number;   // minutes of break inserted after this round (0 = none)
}

export interface SchedulePlan {
  players: number;
  bracketSize: number;  // power of two the main bracket runs at
  prelimMatches: number;
  byes: number;         // players who skip the prelims
  rounds: RoundPlan[];
  totalMinutes: number;         // concurrent play, with breaks
  streamEveryMatchMinutes: number; // every match sequential on one stream
}

export function roundName(matchesInRound: number, isPrelim = false): string {
  if (isPrelim) return "Prelims";
  if (matchesInRound === 1) return "Final";
  if (matchesInRound === 2) return "Semi-finals";
  if (matchesInRound === 4) return "Quarter-finals";
  return `Round of ${matchesInRound * 2}`;
}

export function bracketShape(n: number) {
  const pow = Math.pow(2, Math.floor(Math.log2(Math.max(2, n))));
  const bracketSize = n === pow ? n : pow;
  const prelimMatches = n - bracketSize;          // each prelim match eliminates 1
  const byes = bracketSize - prelimMatches;       // players who skip straight to round 1
  return { bracketSize, prelimMatches, byes };
}

export interface ScheduleOpts {
  concurrent?: number;      // matches running at the same time (default 4)
  matchMinutes?: number;    // real time per match incl. setup (default 20)
  breakMinutes?: number;    // standard between-round break (default 10)
  longBreakMinutes?: number;// midpoint break (default 20)
}

export function buildSchedule(players: number, opts: ScheduleOpts = {}): SchedulePlan {
  const n = Math.max(2, Math.floor(players));
  const concurrent = Math.max(1, opts.concurrent ?? 4);
  const matchMinutes = Math.max(5, opts.matchMinutes ?? 20);
  const breakMinutes = opts.breakMinutes ?? 10;
  const longBreakMinutes = opts.longBreakMinutes ?? 20;

  const { bracketSize, prelimMatches, byes } = bracketShape(n);
  const rounds: RoundPlan[] = [];

  if (prelimMatches > 0) {
    rounds.push({ round: 0, name: "Prelims", matches: prelimMatches, waves: 0, minutes: 0, breakAfter: 0 });
  }
  let m = bracketSize / 2;
  let r = 1;
  while (m >= 1) {
    rounds.push({ round: r, name: roundName(m), matches: m, waves: 0, minutes: 0, breakAfter: 0 });
    m = m / 2;
    r += 1;
  }

  // waves + minutes per round, breaks after every round except the final;
  // the round closest to the midpoint gets the long half-time break.
  const midpoint = Math.floor((rounds.length - 1) / 2);
  let total = 0;
  let streamAll = 0;
  rounds.forEach((rd, i) => {
    rd.waves = Math.ceil(rd.matches / concurrent);
    rd.minutes = rd.waves * matchMinutes;
    rd.breakAfter = i === rounds.length - 1 ? 0 : i === midpoint ? longBreakMinutes : breakMinutes;
    total += rd.minutes + rd.breakAfter;
    streamAll += rd.matches * matchMinutes + rd.breakAfter;
  });

  return {
    players: n, bracketSize, prelimMatches, byes, rounds,
    totalMinutes: total, streamEveryMatchMinutes: streamAll,
  };
}

export function fmtDuration(mins: number): string {
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h === 0) return `${m}m`;
  return m ? `${h}h ${m}m` : `${h}h`;
}
