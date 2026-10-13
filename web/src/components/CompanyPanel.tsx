"use client";
/**
 * CompanyPanel — side panel with company info, a metric chart, and filings.
 *
 * It costs no OpenAI tokens: it calls /api/company and /api/financials,
 * which read EDGAR data (cached) directly, without the LLM.
 *
 * ── useEffect ─────────────────────────────────────────────────────────────
 *  useEffect(fn, [deps]) runs `fn` after React draws the component, and again
 *  whenever a value in `deps` changes. Here: "when the ticker changes, fetch
 *  that company's data". The function it returns is a cleanup that runs
 *  before the next effect — we use it to ignore responses for an old ticker
 *  if the user switches companies quickly (avoids a "race condition").
 *
 * ── Derived state ─────────────────────────────────────────────────────────
 *  Each response is stored together with the request it answers (e.g.
 *  { key: "AMD", data }). While rendering we only *use* it if the key matches
 *  the current ticker; otherwise we show "Loading…". That way we never need
 *  to reset state to null inside the effect, which React's lint rules
 *  discourage because it causes an extra render.
 */

import { useEffect, useState } from "react";
import MetricChart from "./MetricChart";
import { formatChange, formatValue, shortDate } from "@/lib/format";
import type { CompanyInfo, CompanySnapshot, MetricSeries, SnapshotFigure } from "@/lib/types";

const METRICS = [
  { id: "revenue", label: "Revenue" },
  { id: "net-income", label: "Net income" },
  { id: "operating-income", label: "Operating" },
  { id: "eps", label: "EPS" },
];

/** fetch() + JSON + error message from FastAPI's {"detail": "..."} in one helper. */
async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  const body = await res.json();
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
  return body as T;
}

/** A fetched result tagged with the request it belongs to. Exactly one of data / error is set. */
type Loaded<T> = { key: string; data?: T; error?: string };

function SnapshotCard({ figure }: { figure: SnapshotFigure }) {
  const change = figure.change;
  return (
    <div className="rounded-lg bg-slate-950 px-3 py-2">
      <p className="text-[11px] text-slate-500">{figure.label}</p>
      <p className="text-sm font-medium text-slate-100">{formatValue(figure.val, figure.unit)}</p>
      {change != null ? (
        <p className={`text-[11px] ${change >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
          {formatChange(change)} vs year ago
        </p>
      ) : (
        <p className="text-[11px] text-slate-500">as of {shortDate(figure.end)}</p>
      )}
    </div>
  );
}

export default function CompanyPanel({ ticker }: { ticker: string | null }) {
  const [metric, setMetric] = useState("revenue");
  const [companyResult, setCompanyResult] = useState<Loaded<CompanyInfo> | null>(null);
  const [snapshotResult, setSnapshotResult] = useState<Loaded<CompanySnapshot> | null>(null);
  const [seriesResult, setSeriesResult] = useState<Loaded<MetricSeries> | null>(null);

  const seriesKey = `${ticker}/${metric}`;

  // Effect 1: load company header + filings whenever the ticker changes.
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    getJson<CompanyInfo>(`/api/company/${encodeURIComponent(ticker)}`)
      .then((data) => !cancelled && setCompanyResult({ key: ticker, data }))
      .catch((e: Error) => !cancelled && setCompanyResult({ key: ticker, error: e.message }));
    return () => {
      cancelled = true;
    };
  }, [ticker]);

  // Effect 2: latest quarter and balance-sheet amounts. Same cache as the chart, no model call.
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    getJson<CompanySnapshot>(`/api/company/${encodeURIComponent(ticker)}/snapshot`)
      .then((data) => !cancelled && setSnapshotResult({ key: ticker, data }))
      .catch((e: Error) => !cancelled && setSnapshotResult({ key: ticker, error: e.message }));
    return () => {
      cancelled = true;
    };
  }, [ticker]);

  // Effect 3: load the chart whenever the ticker or the selected metric changes.
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    getJson<MetricSeries>(`/api/financials/${encodeURIComponent(ticker)}/${metric}`)
      .then((data) => !cancelled && setSeriesResult({ key: seriesKey, data }))
      // Some companies don't tag every metric; the chart area just says "No data".
      .catch((e: Error) => !cancelled && setSeriesResult({ key: seriesKey, error: e.message }));
    return () => {
      cancelled = true;
    };
  }, [ticker, metric, seriesKey]);

  // Derived values: ignore results that belong to a previous ticker/metric.
  const current = companyResult?.key === ticker ? companyResult : null;
  const company = current?.data ?? null;
  const error = current?.error ?? null;
  const currentSeries = seriesResult?.key === seriesKey ? seriesResult : null;
  const series = currentSeries?.data ?? null;
  const snapshot = snapshotResult?.key === ticker ? snapshotResult.data ?? null : null;
  const figures = snapshot ? [snapshot.revenue, snapshot.net_income, snapshot.cash, snapshot.debt].filter((f) => f != null) : [];

  if (!ticker) {
    return (
      <aside className="rounded-2xl border border-dashed border-slate-800 p-5 text-sm text-slate-500">
        Company details appear here once the agent looks up a ticker.
      </aside>
    );
  }

  return (
    <aside className="space-y-5 rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      <header>
        <p className="font-mono text-xs text-sky-400">{ticker}</p>
        <h2 className="text-lg font-semibold text-slate-100">{company?.company ?? (error ? "—" : "Loading…")}</h2>
        {company?.fiscal_year_label && <p className="mt-1 text-xs text-slate-400">{company.fiscal_year_label}</p>}
        {error && <p className="mt-1 text-xs text-rose-400">{error}</p>}
      </header>

      {figures.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {figures.map((figure) => (
            <SnapshotCard key={figure.label} figure={figure} />
          ))}
        </div>
      )}

      <div>
        {/* Tab buttons: clicking one updates `metric`, which re-runs Effect 3. */}
        <div className="mb-3 flex gap-1 rounded-lg bg-slate-950 p-1">
          {METRICS.map((m) => (
            <button
              key={m.id}
              onClick={() => setMetric(m.id)}
              className={`flex-1 rounded-md px-2 py-1 text-xs ${
                metric === m.id ? "bg-slate-800 text-slate-100" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
        {series ? (
          <>
            <MetricChart series={series} />
            <p className="mt-1 text-[11px] text-slate-500 [overflow-wrap:anywhere]">
              Quarterly values from XBRL tag <span className="font-mono">{series.concept}</span>.
              {metric === "eps"
                ? " Q4 appears only when the company tagged a 3-month figure."
                : " When Q4 was not tagged, it is the full year minus the nine-month figure."}
            </p>
          </>
        ) : (
          <div className="flex h-48 items-center justify-center text-xs text-slate-500">
            {currentSeries?.error ? "No quarterly data for this metric" : "Loading chart…"}
          </div>
        )}
      </div>

      {company && (
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Recent filings</h3>
          <ul className="space-y-1">
            {company.filings.map((f) => (
              <li key={f.accession}>
                <a
                  href={f.url}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center justify-between rounded-lg px-2 py-1.5 text-sm hover:bg-slate-800/60"
                >
                  <span className="text-slate-200">
                    <span className="mr-2 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-300">{f.form}</span>
                    {f.fiscal_period}
                  </span>
                  <span className="text-xs text-slate-500">filed {f.filed}</span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  );
}
