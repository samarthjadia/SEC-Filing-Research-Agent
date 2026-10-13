# Research guide

How to pick the right source for a question. This file is loaded into the
agent's instructions at startup; edit it freely without touching Python.

## Time periods

- Always reason in **fiscal periods** (e.g. `FY2027 Q1`), not calendar dates.
  `get_company_filings` returns a `fiscal_period` for every filing, taken from the
  company's own tags, plus a `fiscal_year_note` describing its fiscal year.
- Many companies (NVDA, COST, AAPL, MSFT, ...) have fiscal years that do not match the
  calendar. Read `fiscal_year_note` before mapping a calendar date ("spring 2026") to a fiscal quarter.
- "Latest" / "most recent" / "last quarter" = the filing with the newest `fiscal_period`.
  Filings are returned newest first.
- There is no 10-Q for Q4. A 10-K (labelled `Q4`) covers the full year; Q4-only numbers
  may need full year minus the first nine months.

## Which source answers what

| Question is about | Go to first | Then, if needed |
|---|---|---|
| An exact reported number (revenue, net income, EPS, cash) | `get_company_financials` | 10-Q / 10-K financial statements |
| Headline results, what management highlighted | 10-Q / 10-K MD&A "Overview" | `get_company_financials` |
| Segment breakdown (Data Center, Gaming, ...) | 10-Q / 10-K segment note | MD&A |
| Why results changed (drivers, explanations) | 10-Q / 10-K "Management's Discussion and Analysis" | — |
| Risks | 10-K "Risk Factors" | 10-Q "Risk Factors" (updates only) |
| Liquidity, debt, cash flow, capex | 10-Q / 10-K "Liquidity and Capital Resources" | `get_company_financials` |
| Business overview, strategy, products, competitors | 10-K "Business" | — |
| Full-year or Q4 results | 10-K | `get_company_financials` |
| Guidance / next-quarter outlook, non-GAAP figures, executive quotes | Not available (these are in press releases, which are not loaded) | Say so; offer what the filings do say |

## Searching filing text

`search_company_report` searches by meaning. Pass a short plain-language question
("what drove the change in revenue"), not a regular expression and not a list of keywords.

The search looks in the sections that usually answer that kind of question:

- Revenue, margins, drivers, liquidity, cash → MD&A, and also the financial-statement notes
- Risks → Risk Factors (10-K Item 1A, or 10-Q Part II Item 1A)
- Debt, leases, commitments → the notes
- Products, competition, strategy → 10-K Item 1 (Business)

Each passage starts with a section label, for example
`[10-Q Part I, Item 2 | Management's Discussion and Analysis]`.
Copy that returned text into `sources`. Do not add a Sources list inside `answer`.

Copy `accession` from `get_company_filings` with both hyphens kept
(`0001045810-26-000075`). Do not join the digits together.

If a number appears in both a filing and `get_company_financials`, cite the tagged number
and use the retrieved passage for context.
If one filing does not contain the answer, try the previous `fiscal_period` before giving up.
