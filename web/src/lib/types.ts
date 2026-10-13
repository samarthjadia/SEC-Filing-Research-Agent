/**
 * Shared TypeScript types for data coming from the Python backend.
 *
 * Simply: these describe the "shape" of the JSON the server sends, so the
 * editor can autocomplete fields and warn you about typos (e.g. `answr`).
 *
 * Technically: TypeScript types exist only at compile time — they are erased
 * from the JavaScript that runs in the browser. They must be kept in sync by
 * hand with the Python side:
 *   - AgentEvent      ↔ stream_run() in agents/agent.py
 *   - AgentResponse   ↔ the AgentResponse / Source Pydantic models
 *   - CompanyInfo     ↔ get_company_filings() in utils/tools.py
 *   - MetricSeries    ↔ /financials in agents/server.py
 */

// ---------------------------------------------------------------------------
// Final structured answer (AgentResponse in Python)
// ---------------------------------------------------------------------------

export type Source = {
  document: string; // "AMD 10-Q FY2026 Q2"
  accession: string | null; // "0000002488-26-000123"
  url: string | null; // link to the filing on sec.gov
  excerpt: string; // quote copied from a tool result
};

export type AgentResponse = {
  answer: string; // markdown prose
  sources: Source[];
  confidence: number; // 0.0 – 1.0
};

export type Usage = {
  requests: number; // how many LLM calls the run made
  input_tokens: number;
  output_tokens: number;
};

// ---------------------------------------------------------------------------
// Streamed events (one per SSE frame). Every event has seq, type, ts.
// `type` is a "discriminant": checking event.type === "tool_call" lets
// TypeScript know which other fields exist on that event.
// ---------------------------------------------------------------------------

type EventBase = { seq: number; ts: string };

export type AgentEvent =
  | (EventBase & { type: "run_started"; question: string; model: string })
  | (EventBase & { type: "step"; step: number })
  | (EventBase & { type: "text_delta"; step: number; text: string })
  | (EventBase & { type: "tool_call"; step: number; id: string; name: string; args: Record<string, unknown> })
  | (EventBase & {
      type: "tool_result" | "tool_retry";
      step: number;
      id: string;
      preview: string;
      passages?: string[]; // full chunks from search_company_report
    })
  | (EventBase & { type: "final"; output: AgentResponse; usage: Usage })
  | (EventBase & { type: "error"; error: string; message: string });

// ---------------------------------------------------------------------------
// No-LLM endpoints for the company panel
// ---------------------------------------------------------------------------

export type Filing = {
  accession: string;
  form: "10-K" | "10-Q";
  fiscal_period: string | null; // "FY2026 Q2"
  period_end: string | null; // "2026-06-27"
  filed: string | null;
  url: string;
  use_for: string;
};

export type CompanyInfo = {
  ticker: string;
  company: string | null;
  fiscal_year_label: string | null; // "Fiscal year ends around August 30."
  fiscal_year_note: string | null; // label plus the model instruction; not shown in the UI
  filings: Filing[];
};

export type MetricSeries = {
  ticker: string;
  metric: string;
  label: string; // "Revenue"
  concept: string; // XBRL concept actually used, e.g. "Revenues"
  unit: string; // "USD" or "USD/shares"
  points: { end: string; val: number }[]; // oldest → newest
};

/** One number on the snapshot row. `change` is the year-ago fraction for a quarterly flow, and null for a balance. */
export type SnapshotFigure = {
  label: string;
  concept: string;
  unit: string;
  end: string;
  val: number;
  change: number | null;
};

export type CompanySnapshot = {
  ticker: string;
  revenue: SnapshotFigure | null;
  net_income: SnapshotFigure | null;
  cash: SnapshotFigure | null;
  debt: SnapshotFigure | null;
};
