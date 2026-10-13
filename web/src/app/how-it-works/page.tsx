/**
 * /how-it-works — a static explainer page for portfolio visitors.
 * Pure Server Component: no state, no "use client", just HTML.
 */

import type { Metadata } from "next";

export const metadata: Metadata = { title: "How it works · EDGAR Research Agent" };

const FLOW = [
  { name: "Browser", detail: "Next.js + React UI. Streams events and renders the live trace." },
  { name: "Next.js API route", detail: "Proxies /api/* to the backend. Keeps the backend private; one place for bot checks." },
  { name: "FastAPI", detail: "Rate limits per visitor, daily token budget, streams agent events as Server-Sent Events." },
  { name: "PydanticAI agent", detail: "GPT-5 mini runs a tool loop, then must return a typed answer: prose, sources, and a confidence score it chooses itself." },
  { name: "MCP server", detail: "Tools exposed over the Model Context Protocol, so any MCP client can reuse them." },
  { name: "SEC EDGAR", detail: "Submissions, XBRL company facts, and filing documents, cached on disk." },
  { name: "S3 Vectors", detail: "Filing text is split by section, embedded with OpenAI, and searched by meaning. Exact figures stay in XBRL and are not embedded." },
];

const DECISIONS = [
  {
    title: "Tools over MCP",
    body: "The agent doesn't import its tools; it discovers them from an MCP server it launches. The same server works in Cursor or Claude Desktop unchanged.",
  },
  {
    title: "Fiscal periods from the company's own tags",
    body: "NVIDIA's July 2026 quarter is FY2027 Q2. Rather than guessing calendars, each filing's period comes from the fiscal-period tags in its XBRL data.",
  },
  {
    title: "Exact numbers vs. narrative",
    body: "Figures come from XBRL facts (machine-readable). Explanations come from a section-aware vector search over the filing (MD&A, risk factors, notes). The agent cites the tagged number and uses the retrieved passages for context.",
  },
  {
    title: "Caching that matches how data changes",
    body: "Filing documents never change, so they're cached forever. Filing lists refresh hourly and financial facts every 6 hours. If the SEC is unreachable, stale cache is served.",
  },
  {
    title: "Polite to the SEC",
    body: "Every request carries a contact User-Agent and is throttled to ~8 requests/second, under the SEC's 10/s fair-access limit.",
  },
  {
    title: "Cost guardrails",
    body: "Per-question caps on model calls and tokens, a per-visitor hourly limit, and a global daily token budget.",
  },
  {
    title: "Confidence is a judgment",
    body: "The badge is the model's own number from 0 to 1, not a score computed from search distance or how many sources came back. Green is 75% and up, amber is 40–74%, and red is below 40%.",
  },
];

export default function HowItWorks() {
  return (
    <article className="max-w-3xl space-y-10">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">How it works</h1>
        <p className="mt-2 text-slate-400">
          A retrieval agent that answers questions with live SEC data and shows its work.
        </p>
      </header>

      <section>
        <h2 className="mb-4 text-lg font-semibold">Request flow</h2>
        <ol className="space-y-2">
          {FLOW.map((item, i) => (
            <li key={item.name} className="flex gap-4 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
              <span className="font-mono text-sm text-sky-400">{i + 1}</span>
              <div>
                <p className="font-medium">{item.name}</p>
                <p className="text-sm text-slate-400">{item.detail}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <h2 className="mb-4 text-lg font-semibold">Design decisions</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          {DECISIONS.map((d) => (
            <div key={d.title} className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
              <p className="font-medium">{d.title}</p>
              <p className="mt-1 text-sm text-slate-400">{d.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">Agent tools</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-300">
          <li>
            <code className="font-mono text-sky-300">get_company_filings</code> — recent 10-K / 10-Q filings with fiscal periods
          </li>
          <li>
            <code className="font-mono text-sky-300">search_company_report</code> — meaning search inside one filing. Expand that step in the trace to see the query and the passages.
          </li>
          <li>
            <code className="font-mono text-sky-300">list_company_concepts</code> — which XBRL line items a company reports
          </li>
          <li>
            <code className="font-mono text-sky-300">get_company_financials</code> — exact values for one line item over time
          </li>
        </ul>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">How a filing is searched</h2>
        <ol className="list-decimal space-y-1 pl-5 text-sm text-slate-300">
          <li>Item headings become sections. A 10-K keeps Business, Risk Factors, MD&amp;A, Market Risk, and the financial statements. A 10-Q keeps Part I Items 1–3 and Part II Item 1A, and the label names the part.</li>
          <li>Each section is cut into windows of about 450 words with a 50-word overlap, then embedded. The first search of a filing stores those vectors. Later searches only embed the question.</li>
          <li>The question picks the sections. A revenue question searches MD&amp;A and the notes. A risk question searches Item 1A. If those sections return nothing, the search uses the whole filing.</li>
          <li>Overlapping copies of the same passage collapse to the closer match. Exact figures stay in XBRL and are never embedded.</li>
        </ol>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">Confidence</h2>
        <p className="text-sm text-slate-300">
          The final answer has to include a <code className="font-mono text-sky-300">confidence</code> between 0 and 1.
          The instructions tell the model that 0 means it is guessing or the tools returned nothing, and 1 means the tool results clearly support the answer.
          Pydantic rejects a number outside that range. The page then shows it as a percentage.
          If the model leaves <code className="font-mono text-sky-300">sources</code> empty, one rewrite asks for the same answer with citations, and that second reply can choose a new score.
        </p>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">Limitations</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-400">
          <li>Only 10-K and 10-Q filings: no earnings press releases, guidance, or call transcripts yet.</li>
          <li>US SEC filers only; foreign companies filing 20-F / 6-K aren&apos;t supported.</li>
          <li>A high confidence score can still sit on a wrong reading of a tool result. It is the model&apos;s judgment, and the answer can be wrong. Not investment advice.</li>
        </ul>
      </section>
    </article>
  );
}
