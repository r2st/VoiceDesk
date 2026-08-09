"use client";

/**
 * Dependency-free SVG charts.
 *
 * A charting library would be several hundred kilobytes for two chart shapes,
 * both of which are a handful of scaled coordinates. Rendering the SVG here
 * also keeps the accessible fallback (the `<title>` per mark) under our
 * control rather than a library's.
 */

import { shortDate } from "@/lib/format";

export interface Series {
  date: string;
  value: number;
}

export function BarChart({
  data,
  height = 200,
  label,
  formatValue = (n: number) => String(n),
}: {
  data: Series[];
  height?: number;
  label: string;
  formatValue?: (value: number) => string;
}) {
  if (data.length === 0) {
    return (
      <p className="px-5 py-12 text-center text-sm text-ink-500">
        No data for this period yet.
      </p>
    );
  }

  const max = Math.max(...data.map((d) => d.value), 1);
  // A fixed viewBox with preserveAspectRatio="none" would distort the bars, so
  // the width scales with the number of points instead and the SVG is allowed
  // to stretch responsively.
  const barWidth = 100 / data.length;

  return (
    <div className="px-5 py-4">
      <div className="flex items-end gap-3">
        <span className="text-xs text-ink-500">Peak {formatValue(max)}</span>
      </div>
      <svg
        role="img"
        aria-label={label}
        viewBox={`0 0 100 ${height}`}
        preserveAspectRatio="none"
        className="mt-2 w-full"
        style={{ height }}
      >
        {[0.25, 0.5, 0.75].map((fraction) => (
          <line
            key={fraction}
            x1={0}
            x2={100}
            y1={height * fraction}
            y2={height * fraction}
            stroke="currentColor"
            className="text-ink-100"
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {data.map((point, index) => {
          const barHeight = (point.value / max) * (height - 8);
          return (
            <rect
              key={point.date}
              x={index * barWidth + barWidth * 0.15}
              y={height - barHeight}
              width={barWidth * 0.7}
              height={barHeight}
              rx={0.6}
              className="fill-brand-500"
            >
              <title>{`${shortDate(point.date)}: ${formatValue(point.value)}`}</title>
            </rect>
          );
        })}
      </svg>
      <div className="mt-2 flex justify-between text-xs text-ink-400">
        <span>{shortDate(data[0]?.date ?? null)}</span>
        <span>{shortDate(data[data.length - 1]?.date ?? null)}</span>
      </div>
    </div>
  );
}

export function DistributionBars({
  items,
  formatValue = (n: number) => String(n),
}: {
  items: { label: string; value: number }[];
  formatValue?: (value: number) => string;
}) {
  if (items.length === 0) {
    return (
      <p className="px-5 py-10 text-center text-sm text-ink-500">
        Nothing recorded yet.
      </p>
    );
  }
  const max = Math.max(...items.map((i) => i.value), 1);

  return (
    <ul className="space-y-3 px-5 py-4">
      {items.map((item) => (
        <li key={item.label}>
          <div className="flex items-baseline justify-between text-sm">
            <span className="text-ink-700">{item.label}</span>
            <span className="tnum text-ink-500">{formatValue(item.value)}</span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-ink-100">
            <div
              className="h-full rounded-full bg-brand-500"
              style={{ width: `${(item.value / max) * 100}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
