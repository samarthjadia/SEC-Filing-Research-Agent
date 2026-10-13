# SEC Filing Research Agent

An agent that answers questions about US public companies from live SEC filings, and shows the work behind the answer.

Reported numbers come from XBRL, the machine-readable facts companies file. Explanations come from the 10-K and 10-Q themselves: what management wrote about revenue, risk, liquidity, and the business. The two paths stay separate, so a figure in the answer is the tagged value and the prose around it is a passage from the filing.

https://github.com/user-attachments/assets/c9b55579-8498-4a22-b2a1-1ec61c850adb

## A question, end to end

The browser streams the run. Each tool call appears as it happens. Opening a search shows the query and the passages that supported the reply.

```
Browser  →  Next.js  →  FastAPI  →  PydanticAI agent
                                        │
                                        ▼
                                   MCP tool server
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
        Filing list              Exact XBRL facts          Filing text
                                                              │
                                                              ▼
                                                    section-aware vectors
                                                    Amazon S3 Vectors
```

## Retrieval

A filing is not embedded as one blob.

Item headings become sections: Business, Risk Factors, MD&A, market risk, and the financial-statement notes. A 10-Q label includes the part, so Part II Item 1A is not confused with a Part I item. Each section is split into overlapping windows, embedded, and stored. The question is embedded once. A revenue question searches MD&A and the notes. A risk question searches risk factors. Overlapping copies of the same passage collapse to the closer match.

The first look at a filing stores it. Later questions on that filing search what is already there.

## What the agent can use

| Tool | Role |
| --- | --- |
| `get_company_filings` | Recent 10-K and 10-Q filings, with fiscal period |
| `search_company_report` | Closest passages from one filing |
| `list_company_concepts` | The line items a company actually reports |
| `get_company_financials` | The tagged values for one line item |

The final answer is structured: prose, one citation per filing, and a confidence score the model chooses from the evidence it was given.

## Stack

Python agent on PydanticAI, tools exposed over MCP, FastAPI streaming API, Next.js interface. Embeddings are OpenAI. Vectors live in Amazon S3 Vectors. Filings are read from SEC EDGAR, cached, and requested within the SEC's fair-access limit.

## Scope

10-K and 10-Q filings for US SEC filers. Earnings releases, guidance, and call transcripts are outside this project. Answers are model-generated and can be wrong. This is not investment advice.
