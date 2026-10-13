"use client";
/**
 * ResearchApp — the main screen. It owns the agent state and wires the pieces together.
 *
 *   ┌───────────── left column ─────────────┐ ┌── right column ──┐
 *   │ AskPanel     (type / pick a question) │ │ CompanyPanel     │
 *   │ AgentTrace   (live tool calls)        │ │ (chart, filings) │
 *   │ AnswerCard   (answer + sources)       │ │                  │
 *   └───────────────────────────────────────┘ └──────────────────┘
 *
 * "Lifting state up": the agent state lives here (not inside AskPanel) because
 * several children need it. Children receive it via props and report user
 * actions back via callback props (onAsk, onStop). Data flows down, events flow up.
 */

import { useMemo } from "react";
import AgentTrace from "./AgentTrace";
import AnswerCard from "./AnswerCard";
import AskPanel from "./AskPanel";
import CompanyPanel from "./CompanyPanel";
import { useAgentStream } from "@/lib/useAgentStream";

export default function ResearchApp() {
  const { state, ask, stop } = useAgentStream();

  // The company panel follows whichever ticker the agent most recently looked up.
  // useMemo recomputes this only when `state.steps` changes, not on every render.
  const ticker = useMemo(() => {
    const calls = state.steps.flatMap((s) => s.calls);
    for (let i = calls.length - 1; i >= 0; i--) {
      const t = calls[i].args.ticker;
      if (typeof t === "string" && t) return t.toUpperCase();
    }
    return null;
  }, [state.steps]);

  return (
    // Tailwind: 1 column on phones, 3 columns from the `lg` breakpoint (≥1024px), where the left part spans 2.
    <div className="grid gap-6 lg:grid-cols-3">
      <div className="space-y-6 lg:col-span-2">
        <AskPanel running={state.status === "running"} onAsk={ask} onStop={stop} />
        <AgentTrace state={state} />
        <AnswerCard state={state} />
      </div>
      <div className="min-w-0 lg:sticky lg:top-6 lg:self-start">
        <CompanyPanel ticker={ticker} />
      </div>
    </div>
  );
}
