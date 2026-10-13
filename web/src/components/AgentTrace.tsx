"use client";
/**
 * AgentTrace — a live timeline of what the agent is doing.
 *
 * Each "step" is one call to the LLM. Inside a step, the model may request
 * several tools at once (they run in parallel), so we list them together.
 * A row is expandable (<details>) to reveal the raw tool name, arguments,
 * and a preview of what the tool returned — useful for debugging and for
 * showing reviewers the agent isn't a black box.
 */

import { describeToolCall } from "@/lib/format";
import type { RunState, ToolCallView } from "@/lib/useAgentStream";

function seconds(call: ToolCallView): string {
  if (!call.finishedAt) return "";
  return `${((call.finishedAt - call.startedAt) / 1000).toFixed(1)}s`;
}

/** Small colored status icon: spinner while running, ✓ when done, ↻ when the model must retry. */
function StatusIcon({ status }: { status: ToolCallView["status"] }) {
  if (status === "running") {
    return <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-sky-400 border-t-transparent" />;
  }
  if (status === "retry") return <span className="text-amber-400">↻</span>;
  return <span className="text-emerald-400">✓</span>;
}

export default function AgentTrace({ state }: { state: RunState }) {
  // Render nothing before the first question. Returning null = "draw nothing".
  if (state.status === "idle") return null;

  const lastStep = state.steps.at(-1);
  // The model is "thinking" when the newest step has no tool calls yet (or all finished)
  // and the run is still going: it's either choosing tools or writing the answer.
  const thinking =
    state.status === "running" && (!lastStep || lastStep.calls.every((c) => c.status !== "running"));

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-400">Agent trace</h2>

      <ol className="relative space-y-4 border-l border-slate-700 pl-6">
        {state.steps.map((step) => (
          <li key={step.step}>
            {/* The dot on the vertical timeline line */}
            <span className="absolute -left-[5px] mt-1.5 h-2.5 w-2.5 rounded-full bg-sky-500" />
            <p className="text-xs font-medium text-slate-400">Step {step.step}</p>

            {step.calls.length === 0 && (
              <p className="text-sm text-slate-500">
                {state.status === "done" ? "Reasoned" : "Reasoning…"}
              </p>
            )}

            <ul className="mt-1 space-y-1">
              {step.calls.map((call) => (
                <li key={call.id}>
                  <details className="group rounded-lg px-2 py-1 hover:bg-slate-800/60">
                    <summary className="flex cursor-pointer list-none items-center gap-2 text-sm text-slate-200">
                      <StatusIcon status={call.status} />
                      <span className="flex-1">{describeToolCall(call.name, call.args)}</span>
                      <span className="text-xs text-slate-500">{seconds(call)}</span>
                    </summary>
                    <div className="mt-2 space-y-2 pl-5 font-mono text-xs text-slate-400">
                      <p>
                        <span className="text-slate-500">tool:</span> {call.name}
                      </p>
                      <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-slate-950 p-2">
                        {JSON.stringify(call.args, null, 2)}
                      </pre>
                      {call.name === "search_company_report" && call.passages && call.passages.length > 0 ? (
                        <div className="space-y-2">
                          <p>
                            <span className="text-slate-500">query:</span> {String(call.args.query ?? "")}
                          </p>
                          {call.passages.map((passage, index) => (
                            <pre key={`${call.id}-chunk-${index}`} className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2">
                              {passage}
                            </pre>
                          ))}
                        </div>
                      ) : (
                        call.preview && (
                          <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2">
                            {call.preview}
                          </pre>
                        )
                      )}
                    </div>
                  </details>
                </li>
              ))}
            </ul>
          </li>
        ))}

        {thinking && (
          <li className="text-sm text-slate-400">
            <span className="absolute -left-[5px] mt-1.5 h-2.5 w-2.5 animate-pulse rounded-full bg-slate-500" />
            {state.steps.length === 0 ? "Starting…" : "Thinking…"}
          </li>
        )}

        {state.status === "done" && (
          <li className="text-sm text-emerald-300">
            <span className="absolute -left-[5px] mt-1.5 h-2.5 w-2.5 rounded-full bg-emerald-400" />
            Complete
            {state.startedAt !== null && state.finishedAt !== null && (
              <span className="ml-2 text-xs text-slate-500">
                {((state.finishedAt - state.startedAt) / 1000).toFixed(1)}s
              </span>
            )}
          </li>
        )}
      </ol>
    </section>
  );
}
