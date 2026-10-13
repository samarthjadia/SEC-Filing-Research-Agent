"use client";
/**
 * useAgentStream — a custom React hook that runs one agent question and
 * exposes its live progress as React state.
 *
 * ── React basics used here ────────────────────────────────────────────────
 *  • A "hook" is a function starting with `use` that components call to get
 *    state or behaviour. When the state it returns changes, React re-renders
 *    the components using it — that's how the trace updates live.
 *  • useReducer(reducer, initial) holds state that changes in response to
 *    "actions". You never mutate state; the reducer returns a *new* object,
 *    and React compares old vs new to decide what to redraw.
 *  • useRef holds a value that survives re-renders but doesn't trigger them
 *    (we store the AbortController there so "Stop" can cancel the request).
 *  • useCallback memoizes a function so its identity is stable between renders.
 *
 * ── Streaming basics ──────────────────────────────────────────────────────
 *  The backend answers POST /api/ask with Server-Sent Events (SSE): a text
 *  stream where each event looks like
 *        id: 3
 *        event: tool_call
 *        data: {"seq":3,"type":"tool_call",...}
 *        <blank line>
 *  We read the body chunk by chunk with a ReadableStream reader, split on the
 *  blank line, JSON-parse each `data:` line, and dispatch it to the reducer.
 *  (The browser's EventSource API can't send POST bodies, so we parse by hand.)
 */

import { useCallback, useReducer, useRef } from "react";
import type { AgentEvent, AgentResponse, Usage } from "./types";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

export type ToolCallView = {
  id: string; // pairs a tool_call with its tool_result
  name: string;
  args: Record<string, unknown>;
  status: "running" | "done" | "retry";
  preview?: string; // first ~300 chars of the tool's output
  passages?: string[]; // full search chunks, when this call was search_company_report
  startedAt: number; // ms timestamps (client clock) for durations
  finishedAt?: number;
};

export type StepView = {
  step: number; // 1, 2, 3 … one per LLM call
  calls: ToolCallView[]; // tools the model asked for in that step (often in parallel)
};

export type RunState = {
  status: "idle" | "running" | "done" | "error";
  question: string;
  steps: StepView[];
  answer: AgentResponse | null;
  usage: Usage | null;
  error: string | null;
  startedAt: number | null;
  finishedAt: number | null;
};

const initialState: RunState = {
  status: "idle",
  question: "",
  steps: [],
  answer: null,
  usage: null,
  error: null,
  startedAt: null,
  finishedAt: null,
};

// Actions = the only ways state can change. Server events are wrapped in "event".
type Action =
  | { kind: "start"; question: string }
  | { kind: "event"; event: AgentEvent }
  | { kind: "fail"; message: string }
  | { kind: "stopped"; message: string };

// ---------------------------------------------------------------------------
// Reducer: (current state, action) → next state. Must be pure: no fetches here.
// ---------------------------------------------------------------------------

function reducer(state: RunState, action: Action): RunState {
  const now = Date.now();

  switch (action.kind) {
    case "start":
      return { ...initialState, status: "running", question: action.question, startedAt: now };

    case "fail":
      return { ...state, status: "error", error: action.message, finishedAt: now };

    case "stopped":
      // Only matters if we were still running (a "final" or "error" event may have already arrived).
      return state.status === "running" ? { ...state, status: "error", error: action.message, finishedAt: now } : state;

    case "event": {
      // Stop already ended this run. A late frame must not add steps or flip status back to done.
      if (state.status !== "running") return state;
      const ev = action.event;
      switch (ev.type) {
        case "step":
          // A new LLM call began. `...state.steps` copies the old array, then we append.
          return { ...state, steps: [...state.steps, { step: ev.step, calls: [] }] };

        case "tool_call": {
          const call: ToolCallView = { id: ev.id, name: ev.name, args: ev.args, status: "running", startedAt: now };
          // Add the call to its step (create the step if we somehow missed its event).
          const steps = state.steps.some((s) => s.step === ev.step)
            ? state.steps.map((s) => (s.step === ev.step ? { ...s, calls: [...s.calls, call] } : s))
            : [...state.steps, { step: ev.step, calls: [call] }];
          return { ...state, steps };
        }

        case "tool_result":
        case "tool_retry": {
          // Find the matching call by id and mark it finished.
          const status = ev.type === "tool_retry" ? "retry" : "done";
          const steps = state.steps.map((s) => ({
            ...s,
            calls: s.calls.map((c) =>
              c.id === ev.id
                ? { ...c, status, preview: ev.preview, passages: ev.passages, finishedAt: now }
                : c,
            ),
          }));
          return { ...state, steps } as RunState;
        }

        case "final":
          return { ...state, status: "done", answer: ev.output, usage: ev.usage, finishedAt: now };

        case "error":
          return { ...state, status: "error", error: ev.message || ev.error, finishedAt: now };

        default:
          return state; // run_started, text_delta: nothing to show
      }
    }
  }
}

// ---------------------------------------------------------------------------
// The hook
// ---------------------------------------------------------------------------

export function useAgentStream() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const abortRef = useRef<AbortController | null>(null);
  // The body reader, so Stop can cancel it. Aborting the fetch alone does not
  // end a stream Next has already started piping.
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null);

  const ask = useCallback(async (question: string) => {
    abortRef.current?.abort(); // cancel any run still in flight
    const previous = readerRef.current;
    readerRef.current = null;
    previous?.cancel().catch(() => {});
    const controller = new AbortController();
    abortRef.current = controller;
    dispatch({ kind: "start", question });

    try {
      const response = await fetch("/api/ask", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question }),
        signal: controller.signal,
      });

      // Non-200 = rejected before streaming began (rate limit, budget, validation).
      // FastAPI puts the reason in {"detail": "..."}.
      if (!response.ok || !response.body) {
        const body = await response.json().catch(() => ({}));
        const detail = typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`;
        dispatch({ kind: "fail", message: detail });
        return;
      }

      // Read the stream: bytes → text → SSE frames → JSON events.
      const reader = response.body.getReader();
      readerRef.current = reader;
      const decoder = new TextDecoder();
      let buffer = ""; // holds a partial frame until its closing blank line arrives

      while (true) {
        if (controller.signal.aborted) break;
        const { value, done } = await reader.read();
        if (done || controller.signal.aborted) break;
        buffer += decoder.decode(value, { stream: true });

        // Each complete frame ends with "\n\n". Anything after the last one is incomplete; keep it.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          if (controller.signal.aborted) break;
          const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
          if (!dataLine) continue;
          const event = JSON.parse(dataLine.slice("data:".length).trim()) as AgentEvent;
          dispatch({ kind: "event", event });
        }
      }
      // Stream closed. If no final/error event came, the server died mid-run.
      // Stop already recorded its own message; this one only covers an unexpected close.
      dispatch({ kind: "stopped", message: "The connection closed before the agent finished." });
    } catch (err) {
      if (controller.signal.aborted) {
        dispatch({ kind: "stopped", message: "Stopped." });
      } else {
        dispatch({ kind: "fail", message: err instanceof Error ? err.message : "Network error" });
      }
    } finally {
      readerRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    const reader = readerRef.current;
    readerRef.current = null;
    // cancel() is what tells Next to stop reading the Python stream.
    reader?.cancel().catch(() => {});
    dispatch({ kind: "stopped", message: "Stopped." });
  }, []);

  return { state, ask, stop };
}
