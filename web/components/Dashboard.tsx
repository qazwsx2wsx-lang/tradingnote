"use client";

import { useCallback, useEffect, useState } from "react";
import BubbleChart from "./BubbleChart";
import Footer from "./Footer";
import HeatTable from "./HeatTable";
import RankingChart from "./RankingChart";
import SectorDrawer from "./SectorDrawer";
import SummaryCards from "./SummaryCards";
import TuYangChart from "./TuYangChart";
import { INVESTORS, type Investor, type SectorRow, type Summary } from "@/lib/types";

export default function Dashboard() {
  const [dates, setDates] = useState<string[]>([]);
  const [date, setDate] = useState<string>("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [sectorsBy, setSectorsBy] = useState<Record<Investor, SectorRow[]> | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/dates")
      .then((r) => r.json())
      .then((d: { dates: string[] }) => {
        setDates(d.dates);
        if (d.dates.length) setDate(d.dates[0]);
        else setError("資料庫還沒有法人資料，請先執行 python scripts/backfill.py");
      })
      .catch(() => setError("無法讀取資料庫"));
  }, []);

  useEffect(() => {
    if (!date) return;
    let alive = true;
    setSummary(null);
    setSectorsBy(null);
    fetch(`/api/summary?date=${date}`).then((r) => r.json()).then((s) => alive && setSummary(s));
    Promise.all(
      INVESTORS.map((inv) =>
        fetch(`/api/sectors?date=${date}&investor=${inv}`).then((r) => r.json()).then((d) => [inv, d.sectors] as const),
      ),
    ).then((pairs) => alive && setSectorsBy(Object.fromEntries(pairs) as Record<Investor, SectorRow[]>));
    return () => {
      alive = false;
    };
  }, [date]);

  const closeDrawer = useCallback(() => setSelected(null), []);

  return (
    <main className="mx-auto max-w-6xl space-y-4 px-4 py-5 sm:py-8">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-extrabold sm:text-3xl">法人資金流去哪？</h1>
            <span className="rounded-md bg-accent px-2 py-0.5 text-xs font-semibold text-white">盤後</span>
          </div>
          <p className="muted mt-1 text-sm">
            每天盤後整理外資、投信、自營商在各類股的買賣超，一眼看出資金正在加碼或撤出哪些類股。
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <span className="muted">資料日期</span>
          <select
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="card num px-2 py-1.5"
            disabled={!dates.length}
          >
            {dates.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </label>
      </header>

      {error ? (
        <div className="card p-6 text-center">{error}</div>
      ) : (
        <>
          <SummaryCards summary={summary} />
          <BubbleChart sectorsBy={sectorsBy} date={date} onSelect={setSelected} />
          <RankingChart sectorsBy={sectorsBy} />
          <TuYangChart sectorsBy={sectorsBy} onSelect={setSelected} />
          <HeatTable sectorsBy={sectorsBy} onSelect={setSelected} />
        </>
      )}
      <Footer />
      <SectorDrawer sector={selected} date={date} onClose={closeDrawer} />
    </main>
  );
}
