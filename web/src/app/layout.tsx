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
      <body className="min-h-full bg-slate-950 text-slate-100">
        <header className="border-b border-slate-800">
          <nav className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
            {/* <Link> navigates without a full page reload (client-side routing). */}
            <Link href="/" className="font-semibold tracking-tight">
              EDGAR Research Agent
            </Link>
            <div className="flex gap-5 text-sm text-slate-400">
              <Link href="/" className="hover:text-slate-100">
                Research
              </Link>
              <Link href="/how-it-works" className="hover:text-slate-100">
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
