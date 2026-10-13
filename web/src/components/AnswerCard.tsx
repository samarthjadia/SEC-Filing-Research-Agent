"use client";
/**
 * AnswerCard — the agent's final answer, its confidence, sources, and cost.
 *
 * The answer is markdown (bullets, bold), so react-markdown converts it into
 * real HTML elements. It does NOT render raw HTML from the model by default,
 * which protects against script injection.
 */

import Markdown from "react-markdown";
import { estimateCost } from "@/lib/format";
import type { RunState } from "@/lib/useAgentStream";

/** Green / amber / red pill depending on how sure the agent says it is. */
function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color =
    value >= 0.75
      ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/40"
      : value >= 0.4
        ? "bg-amber-500/15 text-amber-300 border-amber-500/40"
        : "bg-rose-500/15 text-rose-300 border-rose-500/40";
  return (
    <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${color}`} title="Agent's self-reported confidence">
      {pct}% confidence
    </span>
  );
}

export default function AnswerCard({ state }: { state: RunState }) {
  // Error card: rate limit, budget exhausted, unknown ticker, backend down…
  if (state.status === "error") {
    return (
      <section className="rounded-2xl border border-rose-900 bg-rose-950/40 p-5 text-rose-200">
        <h2 className="mb-1 font-semibold">Couldn&apos;t finish that one</h2>
        <p className="text-sm">{state.error}</p>
      </section>
    );
  }

  if (state.status !== "done" || !state.answer) return null;

  const { answer, sources, confidence } = state.answer;
  const elapsed = state.startedAt && state.finishedAt ? ((state.finishedAt - state.startedAt) / 1000).toFixed(0) : "?";

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      <div className="mb-3 flex items-center justify-between gap-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Answer</h2>
        <ConfidenceBadge value={confidence} />
      </div>

      {/* .answer-markdown styles live in globals.css */}
      <div className="answer-markdown text-slate-100">
        <Markdown>{answer}</Markdown>
      </div>

      {sources.length > 0 && (
        <div className="mt-6">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Sources</h3>
          <ul className="space-y-2">
            {sources.map((source) => (
              <li key={source.accession ?? source.document} className="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium text-slate-200">{source.document}</span>
                  {source.url && (
                    // target=_blank opens a new tab; rel=noreferrer is a standard safety pairing with it.
                    <a href={source.url} target="_blank" rel="noreferrer" className="shrink-0 text-xs text-sky-400 hover:underline">
                      View on SEC.gov ↗
                    </a>
                  )}
                </div>
                <blockquote className="mt-2 border-l-2 border-slate-700 pl-3 text-sm text-slate-400">
                  {source.excerpt}
                </blockquote>
              </li>
            ))}
          </ul>
        </div>
      )}

      {state.usage && (
        <p className="mt-5 text-xs text-slate-500">
          {state.usage.requests} model calls · {(state.usage.input_tokens + state.usage.output_tokens).toLocaleString()} tokens ·
          ~{estimateCost(state.usage.input_tokens, state.usage.output_tokens)} · {elapsed}s
        </p>
      )}
    </section>
  );
}
