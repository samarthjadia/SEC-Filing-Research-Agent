/**
 * Proxy: forwards /api/* requests from the browser to the Python FastAPI backend.
 *
 * Simply: the browser only ever talks to this Next.js app. When it calls
 * /api/ask, this file passes the request along to http://127.0.0.1:8000/ask
 * and pipes the answer back. Visitors never see the backend's address.
 *
 * Technically:
 *   - This is a Next.js "Route Handler". The folder name `[...path]` is a
 *     catch-all: /api/company/AMD gives params.path = ["company", "AMD"].
 *   - It runs on the server (Node.js), never in the browser, so it can read
 *     BACKEND_URL from the environment without exposing it.
 *   - The upstream response body is a ReadableStream. Passing it straight into
 *     `new Response(...)` streams bytes through as they arrive — essential for
 *     the live agent trace (Server-Sent Events).
 *   - Why a proxy instead of calling FastAPI directly from the browser?
 *       1. No CORS setup (browser and API are the same origin).
 *       2. One place to add bot checks / auth later.
 *       3. The backend can stay private on your host's internal network.
 */

import type { NextRequest } from "next/server";

// Where FastAPI is running. Set in web/.env.local (dev) or your host's dashboard (prod).
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

// Only these backend paths can be reached through the proxy.
const ALLOWED_PREFIXES = ["ask", "company", "financials", "health"];

async function forward(request: NextRequest, ctx: RouteContext<"/api/[...path]">): Promise<Response> {
  // In Next.js 15+ `params` is a Promise, so it must be awaited.
  const { path } = await ctx.params;

  if (!ALLOWED_PREFIXES.includes(path[0])) {
    return Response.json({ detail: "Not found" }, { status: 404 });
  }

  // Rebuild the target URL: same path segments + same ?query string.
  const target = new URL(`${BACKEND_URL}/${path.map(encodeURIComponent).join("/")}`);
  target.search = request.nextUrl.search;

  // Tell the backend who the real visitor is, so per-IP rate limits work.
  const visitorIp = request.headers.get("x-forwarded-for")?.split(",")[0].trim() ?? "127.0.0.1";

  // Separate from request.signal. Stop cancels the response body, and that
  // cancel() below aborts this controller so FastAPI sees the client leave.
  const upstreamAbort = new AbortController();
  const abortUpstream = () => upstreamAbort.abort();
  request.signal.addEventListener("abort", abortUpstream, { once: true });

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers: {
        "content-type": request.headers.get("content-type") ?? "application/json",
        "x-forwarded-for": visitorIp,
      },
      // GET requests can't have a body; for POST we pass the JSON text through.
      body: request.method === "GET" ? undefined : await request.text(),
      cache: "no-store", // never cache: answers and rate limits are per request
      signal: upstreamAbort.signal,
    });
  } catch {
    if (upstreamAbort.signal.aborted) {
      return new Response(null, { status: 499 });
    }
    return Response.json(
      { detail: "Backend is not reachable. Is `uvicorn agents.server:app` running?" },
      { status: 502 },
    );
  }

  const contentType = upstream.headers.get("content-type") ?? "application/json";
  const headers = {
    "content-type": contentType,
    // "no-transform" stops Next's gzip compression from buffering the
    // event stream (which would make the trace appear all at once at the end).
    "cache-control": "no-cache, no-transform",
  };

  if (!upstream.body) {
    return new Response(null, { status: upstream.status, headers });
  }

  const reader = upstream.body.getReader();
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      const { done, value } = await reader.read();
      if (done) {
        controller.close();
        return;
      }
      controller.enqueue(value);
    },
    cancel() {
      abortUpstream();
      return reader.cancel();
    },
  });

  return new Response(stream, { status: upstream.status, headers });
}

// A route file exports one function per HTTP method it supports.
export const GET = forward;
export const POST = forward;
