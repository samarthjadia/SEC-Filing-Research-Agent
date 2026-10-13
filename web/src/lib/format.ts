/**
 * Small pure helpers for turning raw data into human-friendly text.
 * "Pure" = no state, no side effects: same input → same output. Easy to test.
 */

/** 2297000000 → "$2.30B";  0.54 with unit "USD/shares" → "$0.54". */
export function formatValue(val: number, unit: string): string {
  if (unit.includes("/shares")) return `$${val.toFixed(2)}`; // per-share values are small
  const abs = Math.abs(val);
  const sign = val < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
  return `${sign}$${abs.toLocaleString()}`;
}

/** 0.124 → "+12.4%";  -0.031 → "-3.1%". */
export function formatChange(change: number): string {
  const pct = change * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(1)}%`;
}

/** "2026-06-27" → "Jun '26" (short label for chart axes). */
export function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`); // add a time so it's parsed as local, not UTC midnight
  return d.toLocaleDateString("en-US", { month: "short" }) + ` '${String(d.getFullYear()).slice(2)}`;
}

/**
 * Turn a raw tool call into a sentence a visitor understands.
 * The raw name + args are still shown when the row is expanded.
 */
export function describeToolCall(name: string, args: Record<string, unknown>): string {
  const ticker = String(args.ticker ?? "").toUpperCase();
  switch (name) {
    case "get_company_filings":
      return `Listed ${ticker}'s 10-K and 10-Q filings`;
    case "search_company_report":
      return `Searched a ${ticker} filing for “${String(args.query ?? args.regexPattern ?? "")}”`;
    case "list_company_concepts":
      return `Looked up ${ticker}'s reported line items${args.query ? ` matching “${args.query}”` : ""}`;
    case "get_company_financials":
      return `Pulled ${ticker} ${String(args.concept_id ?? "")} figures`;
    default:
      return name;
  }
}

// Approximate gpt-5-nano list prices in USD per 1M tokens.
// These are estimates for display only — check OpenAI's pricing page and update if they change.
const PRICE_PER_M_INPUT = 0.05;
const PRICE_PER_M_OUTPUT = 0.4;

/** Estimated dollar cost of one run, e.g. "$0.0024". */
export function estimateCost(inputTokens: number, outputTokens: number): string {
  const usd = (inputTokens * PRICE_PER_M_INPUT + outputTokens * PRICE_PER_M_OUTPUT) / 1_000_000;
  return `$${usd.toFixed(4)}`;
}
