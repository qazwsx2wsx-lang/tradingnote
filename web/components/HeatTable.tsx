"use client";

import { useMemo, useState } from "react";
import InvestorTabs from "./InvestorTabs";
import Section from "./Section";
import { fmtPct, fmtYi, streakLabel } from "@/lib/format";
import type { Investor, SectorRow } from "@/lib/types";

type Key = "sector" | "day_amt" | "sum5" | "accel" | "sum20" | "streak" | "change5_pct";

const COLUMNS: { key: Key; label: string }[] = [
  { key: "sector", label: "類股" },
  { key: "day_amt", label: "當日買賣超(億)" },
  { key: "sum5", label: "近5日買賣超(億)" },
  { key: "accel", label: "加速流入(億/天)" },
  { key: "sum20", label: "近20日買賣超(億)" },
  { key: "streak", label: "連續買賣" },
  { key: "change5_pct", label: "近5日漲跌" },
];

/** 依數值相對該欄最大絕對值，上紅（正）綠（負）底色，深淺代表強度 */
function heat(value: number | null, max: number): React.CSSProperties {
  if (!value || !max) return {};
  const alpha = 0.08 + 0.5 * Math.min(1, Math.abs(value) / max);
  const rgb = value > 0 ? "239,68,68" : "34,197,94";
  return { backgroundColor: `rgba(${rgb},${alpha.toFixed(3)})` };
}

export default function HeatTable({
  sectorsBy,
  onSelect,
}: {
  sectorsBy: Record<Investor, SectorRow[]> | null;
  onSelect: (sector: string) => void;
}) {
  const [investor, setInvestor] = useState<Investor>("all");
  const [sort, setSort] = useState<{ key: Key; desc: boolean }>({ key: "day_amt", desc: true });
  const rows = useMemo(() => sectorsBy?.[investor] ?? [], [sectorsBy, investor]);

  const sorted = useMemo(() => {
    const dir = sort.desc ? -1 : 1;
    return [...rows].sort((a, b) => {
      const x = a[sort.key];
      const y = b[sort.key];
      if (typeof x === "string" || typeof y === "string") return String(x).localeCompare(String(y), "zh-Hant") * dir;
      return ((x ?? -Infinity) - (y ?? -Infinity)) * dir;
    });
  }, [rows, sort]);

  const max = useMemo(() => {
    const m: Partial<Record<Key, number>> = {};
    for (const c of COLUMNS) {
      if (c.key === "sector") continue;
      m[c.key] = Math.max(0, ...rows.map((r) => Math.abs((r[c.key] as number | null) ?? 0)));
    }
    return m;
  }, [rows]);

  const cell = (r: SectorRow, key: Key) => {
    const v = r[key] as number | null;
    if (key === "streak") return streakLabel(r.streak);
    if (key === "change5_pct") return fmtPct(v);
    return fmtYi(v);
  };

  return (
    <Section
      title="法人買賣熱力圖"
      subtitle="點欄位標題排序；點一列看類股明細"
      actions={<InvestorTabs value={investor} onChange={setInvestor} />}
    >
      <div className="-mx-4 overflow-x-auto sm:mx-0">
        <table className="num w-full min-w-[640px] text-sm">
          <thead>
            <tr className="border-b border-[var(--border)]">
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  onClick={() => setSort((s) => ({ key: c.key, desc: s.key === c.key ? !s.desc : c.key !== "sector" }))}
                  className={`muted cursor-pointer select-none whitespace-nowrap px-2 py-2 text-xs font-medium ${
                    c.key === "sector" ? "sticky left-0 bg-[var(--card)] text-left" : "text-right"
                  }`}
                >
                  {c.label}
                  {sort.key === c.key ? (sort.desc ? " ▼" : " ▲") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr
                key={r.sector}
                onClick={() => onSelect(r.sector)}
                className="cursor-pointer border-b border-[var(--border)] last:border-0 hover:opacity-80"
              >
                {COLUMNS.map((c) =>
                  c.key === "sector" ? (
                    <td key={c.key} className="sticky left-0 whitespace-nowrap bg-[var(--card)] px-2 py-1.5 font-medium">
                      {r.sector}
                    </td>
                  ) : (
                    <td
                      key={c.key}
                      className="whitespace-nowrap px-2 py-1.5 text-right"
                      style={heat(c.key === "streak" ? r.streak : (r[c.key] as number | null), max[c.key] ?? 0)}
                    >
                      {cell(r, c.key)}
                    </td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
