"use client";
/**
 * AskPanel — the question box, example chips, and Ask/Stop button.
 *
 * "use client" (first line) marks this as a Client Component: it runs in the
 * browser, so it can use state (useState) and react to clicks/typing.
 * Components without it are Server Components, rendered to HTML on the server.
 *
 * Props: data passed in from the parent, like function arguments.
 * This component doesn't run the agent itself; it calls `onAsk(question)`
 * and the parent (ResearchApp) decides what happens. That keeps it reusable.
 */

import { useState } from "react";

const EXAMPLES = [
  "What drove NVIDIA's revenue growth in its latest quarter?",
  "Compare AMD's net income over its last four quarters.",
  "What are Costco's biggest risk factors in its latest 10-K?",
  "How much cash and debt does Apple have right now?",
];

const MAX_CHARS = 500; // matches MAX_QUESTION_CHARS in agents/server.py

type Props = {
  running: boolean; // is a question currently being answered?
  onAsk: (question: string) => void;
  onStop: () => void;
};

export default function AskPanel({ running, onAsk, onStop }: Props) {
  // `question` is the textbox's current text; setQuestion updates it and re-renders.
  const [question, setQuestion] = useState("");

  function submit(text: string) {
    const trimmed = text.trim();
    if (!trimmed || running) return;
    setQuestion(trimmed);
    onAsk(trimmed);
  }

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      {/* A <form> lets Enter submit. preventDefault stops the browser's default full-page reload. */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit(question);
        }}
        className="flex flex-col gap-3"
      >
        <label htmlFor="question" className="text-sm font-medium text-slate-300">
          Ask about any US public company
        </label>
        <div className="flex gap-2">
          <textarea
            id="question"
            value={question} // "controlled input": React state is the source of truth for the text
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              // Enter submits; Shift+Enter inserts a newline.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit(question);
              }
            }}
            maxLength={MAX_CHARS}
            rows={2}
            placeholder="e.g. What did Microsoft's latest 10-Q say about cloud revenue?"
            className="flex-1 resize-none rounded-xl border border-slate-700 bg-slate-950 px-4 py-3 text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none"
          />
          {running ? (
            <button
              type="button"
              onClick={onStop}
              className="rounded-xl bg-slate-700 px-5 font-medium text-slate-100 hover:bg-slate-600"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!question.trim()}
              className="rounded-xl bg-sky-600 px-5 font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Ask
            </button>
          )}
        </div>
      </form>

      {/* .map() turns an array of strings into an array of buttons. `key` helps React track each item. */}
      <div className="mt-4 flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            disabled={running}
            onClick={() => submit(example)}
            className="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:border-sky-500 hover:text-sky-300 disabled:opacity-40"
          >
            {example}
          </button>
        ))}
      </div>

      <p className="mt-4 text-xs text-slate-500">
        Answers come from SEC 10-K / 10-Q filings and XBRL financial data. Earnings guidance, press
        releases, and call transcripts are not included. Not investment advice.
      </p>
    </section>
  );
}
