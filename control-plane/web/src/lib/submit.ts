import { useCallback, useRef, useState } from "react";
import { newIdempotencyKey, outcomeUnknown } from "./api";

export type Submission = {
  readonly pending: boolean;
  readonly error: unknown;
  /** Run one write. Resolves with its result, or undefined when it failed (see error). */
  readonly run: <T>(write: (idempotencyKey: string) => Promise<T>) => Promise<T | undefined>;
  readonly clear: () => void;
  /** The failure of the last run, readable right after awaiting it (state lags a render). */
  readonly lastError: () => unknown;
};

/**
 * One write at a time with an idempotency key that survives an unknown outcome: after a
 * network error or 5xx the same key is reused, so a retry cannot apply the write twice.
 * A definite answer (success or 4xx) starts a fresh key for the next attempt.
 */
export function useSubmission(scope: string): Submission {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(undefined);
  const key = useRef<string | null>(null);
  const last = useRef<unknown>(undefined);

  const run = useCallback(
    async <T,>(write: (idempotencyKey: string) => Promise<T>): Promise<T | undefined> => {
      key.current ??= newIdempotencyKey(scope);
      setPending(true);
      setError(undefined);
      last.current = undefined;
      try {
        const result = await write(key.current);
        key.current = null;
        return result;
      } catch (failure) {
        if (!outcomeUnknown(failure)) key.current = null;
        last.current = failure;
        setError(failure);
        return undefined;
      } finally {
        setPending(false);
      }
    },
    [scope],
  );

  const clear = useCallback(() => setError(undefined), []);
  const lastError = useCallback(() => last.current, []);
  return { pending, error, run, clear, lastError };
}
