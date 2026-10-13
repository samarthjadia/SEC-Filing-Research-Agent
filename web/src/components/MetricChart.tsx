"use client";
/**
 * MetricChart — a bar chart of one metric over recent quarters (Recharts).
 *
 * Recharts is "declarative": instead of drawing pixels, you describe the
 * chart as nested components (<BarChart> containing <XAxis>, <Bar>, …) and
 * it renders an SVG. ResponsiveContainer makes it fill its parent's width.
 */

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatValue, shortDate } from "@/lib/format";
import type { MetricSeries } from "@/lib/types";

export default function MetricChart({ series }: { series: MetricSeries }) {
  const data = series.points.map((p) => ({ label: shortDate(p.end), end: p.end, val: p.val }));

  return (
    <div className="h-48 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 4, bottom: 0, left: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
          <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis
            tick={{ fill: "#94a3b8", fontSize: 11 }}
            axisLine={false}
            tickLine={false}
            width={56}
            tickFormatter={(v: number) => formatValue(v, series.unit)}
          />
          <Tooltip
            cursor={{ fill: "#1e293b" }}
            contentStyle={{ background: "#0f172a", border: "1px solid #334155", borderRadius: 8 }}
            labelStyle={{ color: "#cbd5e1" }}
            formatter={(v) => [formatValue(Number(v), series.unit), series.label]}
            labelFormatter={(_label, payload) => `Quarter ended ${payload?.[0]?.payload?.end ?? ""}`}
          />
          <Bar dataKey="val" fill="#0ea5e9" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
