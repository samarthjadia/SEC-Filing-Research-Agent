/**
 * Root layout — wraps every page in the app.
 *
 * In Next.js's App Router, the folder structure under src/app/ *is* the URL map:
 *   src/app/page.tsx                → /
 *   src/app/how-it-works/page.tsx   → /how-it-works
 *   src/app/api/[...path]/route.ts  → /api/* (server-only proxy, no UI)
 * layout.tsx is shared chrome (header, fonts, <html>/<body>) around all of them;
 * `children` is whichever page matches the current URL.
 *
 * This is a Server Component (no "use client"), so it renders to HTML on the
 * server and ships no JavaScript of its own.
 */

import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

// next/font downloads the font at build time and self-hosts it (no request to Google at runtime).
const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

// <title> and <meta description> for the browser tab and link previews.
export const metadata: Metadata = {
  title: "EDGAR Research Agent",
  description: "An AI agent that answers questions about US public companies using live SEC filings.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full text-slate-100">
        <header className="border-b border-slate-800/80 bg-slate-950/70 backdrop-blur">
          <nav className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3.5">
            {/* <Link> navigates without a full page reload (client-side routing). */}
            <Link href="/" className="flex items-center gap-2.5">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-sky-400/30 bg-sky-400/10 text-sky-300">
                <svg viewBox="0 0 16 16" className="h-4 w-4" aria-hidden="true">
                  <path
                    fill="currentColor"
                    d="M3 13.5V8.2h2.2v5.3H3Zm3.9 0V4.6h2.2v8.9H6.9Zm3.9 0V2.5H13v11H10.8Z"
                  />
                </svg>
              </span>
              <span className="leading-none">
                <span className="block text-sm font-semibold tracking-tight">EDGAR</span>
                <span className="mt-0.5 block text-[11px] font-medium tracking-wide text-slate-500">Research Agent</span>
              </span>
            </Link>
            <div className="flex items-center rounded-full border border-slate-800 bg-slate-900/80 p-1 text-sm">
              <Link href="/" className="rounded-full px-3 py-1 text-slate-200 hover:bg-slate-800">
                Research
              </Link>
              <Link href="/how-it-works" className="rounded-full px-3 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100">
                How it works
              </Link>
            </div>
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
