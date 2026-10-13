# Web UI (Next.js + React)

The browser front end for the EDGAR research agent. The project overview
is the root `README.md`.

```bash
cd web
npm install          # first time only: downloads dependencies into node_modules/
cp .env.example .env.local
npm run dev          # http://localhost:3000, reloads when you save a file
```

Other commands: `npm run build` (production build + type check), `npm start`
(serve that build), `npm run lint` (code-quality checks).

## File map

```
web/
├── .env.local                    BACKEND_URL (where FastAPI runs). Not committed.
├── package.json                  Dependencies + npm scripts (like requirements.txt)
└── src/
    ├── app/                      Routes: the folder structure IS the URL structure
    │   ├── layout.tsx            Shared shell around every page (<html>, header nav)
    │   ├── page.tsx              "/"              home page
    │   ├── how-it-works/page.tsx "/how-it-works"  explainer page
    │   ├── api/[...path]/route.ts "/api/*"        server-side proxy to FastAPI
    │   └── globals.css           Tailwind import + markdown styles
    ├── components/               Reusable UI pieces
    │   ├── ResearchApp.tsx       Main screen: owns agent state, lays out the panels
    │   ├── AskPanel.tsx          Question box + example chips
    │   ├── AgentTrace.tsx        Live timeline of tool calls. A search row shows the query and passages
    │   ├── AnswerCard.tsx        Answer, confidence, sources, cost
    │   ├── CompanyPanel.tsx      Company info, chart, filings (no LLM tokens)
    │   └── MetricChart.tsx       Recharts bar chart
    └── lib/
        ├── useAgentStream.ts     Hook: POST /api/ask, parse the event stream, build state
        ├── types.ts              TypeScript shapes of the backend's JSON
        └── format.ts             Number/date formatting, tool-call descriptions
```

## How a question flows through the code

1. **AskPanel**: you type and press Enter, so it calls `onAsk(question)`.
2. **ResearchApp** passed `ask` from **useAgentStream** as `onAsk`, so `ask(question)` runs.
3. **useAgentStream** sends `POST /api/ask` and reads the response stream piece by piece.
4. **api/[...path]/route.ts** (on the Next.js server) forwards it to FastAPI `POST /ask`
   and pipes the streamed bytes back unchanged.
5. Each event (`step`, `tool_call`, `tool_result`, `final`) is `dispatch`ed to the
   hook's **reducer**, which returns a new state object.
6. React notices the new state and re-renders **AgentTrace** and **AnswerCard**.
7. **ResearchApp** reads the ticker from the latest tool call and passes it to
   **CompanyPanel**, whose `useEffect` fetches `/api/company/...` and `/api/financials/...`.
