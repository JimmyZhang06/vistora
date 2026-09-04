"use client";

import { useEffect, useRef } from "react";

export interface SmartPollingOptions {
  enabled?: boolean;
  intervalMs: number;
  runImmediately?: boolean;
}

export function useSmartPolling(callback: () => void | Promise<void>, {
  enabled = true,
  intervalMs,
  runImmediately = false,
}: SmartPollingOptions) {
  const callbackRef = useRef(callback);
  useEffect(() => { callbackRef.current = callback; }, [callback]);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer = 0;

    const schedule = () => {
      timer = window.setTimeout(() => void tick(), intervalMs);
    };
    const tick = async () => {
      if (stopped) return;
      if (!document.hidden) await callbackRef.current();
      if (!stopped) schedule();
    };
    const resume = () => {
      if (document.hidden || stopped) return;
      window.clearTimeout(timer);
      void tick();
    };

    if (runImmediately) void tick();
    else schedule();
    document.addEventListener("visibilitychange", resume);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [enabled, intervalMs, runImmediately]);
}
