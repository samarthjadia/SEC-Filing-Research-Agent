/**
 * Home page (/). A Server Component that renders static intro text, then hands
 * off to <ResearchApp />, a Client Component that handles all interactivity.
 * Keeping the page itself server-rendered means the heading appears instantly.
 */

import ResearchApp from "@/components/ResearchApp";

export default function Home() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">Research any US public company</h1>
        <p className="mt-2 max-w-2xl text-slate-400">
          An AI agent reads live SEC filings and XBRL financial data, shows every step it takes, and cites the exact
          filings behind its answer.
        </p>
      </div>
      <ResearchApp />
    </div>
  );
}
