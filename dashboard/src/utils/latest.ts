// Protection against out-of-order async responses.
//
// Every filterable view refetches from a watcher: type in the search box, flip
// a tab, click page 2 — each keystroke or click fires a request while the
// previous one is still in flight. `await` resolves in *completion* order, not
// in call order, so a slow earlier request can land after a fast later one and
// paint rows that no longer match the filters shown on screen.
//
// On the receipts screen this is not cosmetic: the approve/reject buttons act
// on whatever the detail drawer currently holds, so a stale detail response
// means the operator can credit the wrong wallet.
//
// `latestOnly()` owns one request stream and gives it two properties:
//   * the previous in-flight request is aborted, so the browser frees the
//     connection (it only allows a handful per host) and the server stops
//     working on a result nobody will read,
//   * only the newest request may write to component state. A late loser
//     commits nothing at all — no data, no error message, not even
//     `loading = false`, because the spinner belongs to the request that is
//     still running.
//
// Losers are dropped silently on purpose. A request we cancelled ourselves is
// not a failure the operator should see; surfacing it as "network error" would
// invite a pointless retry.

export function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

export interface LatestHandlers<T> {
  /** Apply the response. Runs only while this request is still the newest. */
  commit: (value: T) => void;
  /** Report a genuine failure. Aborted and stale requests never reach here. */
  fail?: (error: unknown) => void;
  /** Runs after commit/fail, again only for the newest request. */
  settle?: () => void;
}

export interface LatestRunner {
  <T>(
    task: (signal: AbortSignal) => Promise<T>,
    handlers: LatestHandlers<T>,
  ): Promise<void>;
  /**
   * Drop whatever is in flight without starting a replacement — for when the
   * operator closes the panel the answer was destined for.
   *
   * The cancelled request's `settle` will not run, so the caller owns resetting
   * its own loading flag here.
   */
  cancel(): void;
}

/**
 * Create a runner for one logical request stream (one list, one drawer, ...).
 *
 * Use a separate runner per stream: a drawer opening must not cancel the list
 * refresh behind it.
 */
export function latestOnly(): LatestRunner {
  let latest = 0;
  let inflight: AbortController | null = null;

  async function run<T>(
    task: (signal: AbortSignal) => Promise<T>,
    handlers: LatestHandlers<T>,
  ): Promise<void> {
    inflight?.abort();
    const controller = new AbortController();
    inflight = controller;
    const id = ++latest;

    try {
      // `task` is called synchronously, so it captures the filter values as
      // they were at the moment this refresh was requested.
      const value = await task(controller.signal);
      if (id !== latest) return;
      handlers.commit(value);
    } catch (error) {
      if (id !== latest || isAbortError(error)) return;
      handlers.fail?.(error);
    } finally {
      if (id === latest) {
        inflight = null;
        handlers.settle?.();
      }
    }
  }

  run.cancel = function cancel(): void {
    inflight?.abort();
    inflight = null;
    // Bumping the counter matters even after abort(): a response that already
    // arrived is sitting in the microtask queue and would otherwise still be
    // treated as the newest.
    latest += 1;
  };

  return run;
}
