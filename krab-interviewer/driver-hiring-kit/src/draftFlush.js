/** Lets ProgressTrail flush the interview draft before leaving /apply. */
let flushFn = null;

export function registerDraftFlush(fn) {
  flushFn = fn;
}

export function unregisterDraftFlush() {
  flushFn = null;
}

export async function runDraftFlush() {
  if (flushFn) await flushFn();
}
