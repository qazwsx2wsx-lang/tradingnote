"use client";

import { useEffect, useState } from "react";
import InvestorTabs from "./InvestorTabs";
import SectorFlowChart from "./SectorFlowChart";
import { fmtPct, fmtYi, streakLabel, toneClass } from "@/lib/format";
import type { Investor, StockRow } from "@/lib/types";

export default function SectorDrawer({
  sector,
  date,
  onClose,
}: {
  sector: string | null;
  date: string;
  onClose: () => void;
}) {
  const [investor, setInvestor] = useState<Investor>("all");
  const [stocks, setStocks] = useState<StockRow[] | null>(null);

  useEffect(() => {
    if (!sector) return;
    let alive = true;
    setStocks(null);
    fetch(`/api/sectors/${encodeURIComponent(sector)}/stocks?date=${date}&investor=${investor}`)
      .then((r) => r.json())
      .then((d) => alive && setStocks(d.stocks ?? []));
    return () => {
      alive = false;
    };
  }, [sector, date, investor]);

  useEffect(() => {
    if (!sector) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [sector, onClose]);

  if (!sector) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label={`${sector} 明細`}>
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <aside className="relative flex h-full w-full flex-col overflow-y-auto bg-[var(--bg)] shadow-2xl sm:max-w-2xl">
        <header className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-[var(--border)] bg-[var(--bg)] px-4 py-3">
          <div>
            <h2 className="text-lg font-bold">{sector}</h2>
            <p className="muted text-xs">資料日期 {date}</p>
          </div>
          <button onClick={onClose} aria-label="關閉" className="rounded-lg px-3 py-1 text-xl hover:bg-[var(--card)]">
            ✕
          </button>
        </header>
        <div className="space-y-4 p-4">
          <p className="muted text-xs">金額以「張數 × 當日收盤價」估算；近 5 日漲跌為成分股平均</p>
          <InvestorTabs value={investor} onChange={setInvestor} />
          <div className="card p-3">
            <h3 className="mb-1 text-sm font-semibold">近 30 日類股資金流（億／日）</h3>
            <SectorFlowChart sector={sector} investor={investor} date={date} height={220} />
          </div>
          <div className="card overflow-x-auto p-0">
            <table className="num w-full min-w-[520px] text-sm">
              <thead>
                <tr className="muted border-b border-[var(--border)] text-xs">
                  <th className="px-3 py-2 text-left font-medium">代號 名稱</th>
                  <th className="px-2 py-2 text-right font-medium">收盤</th>
                  <th className="px-2 py-2 text-right font-medium">當日漲跌</th>
                  <th className="px-2 py-2 text-right font-medium">當日買超(億)</th>
                  <th className="px-2 py-2 text-right font-medium">外資連買</th>
                  <th className="px-3 py-2 text-right font-medium">20日累計(億)</th>
                </tr>
              </thead>
              <tbody>
                {stocks === null && (
                  <tr><td colSpan={6} className="muted py-6 text-center">載入中…</td></tr>
                )}
                {stocks?.map((s) => (
                  <tr key={s.ticker} className="border-b border-[var(--border)] last:border-0">
                    <td className="whitespace-nowrap px-3 py-1.5">
                      <span className="muted mr-1.5">{s.ticker}</span>{s.name}
                    </td>
                    <td className="px-2 py-1.5 text-right">{s.close?.toFixed(2) ?? "—"}</td>
                    <td className={`px-2 py-1.5 text-right ${toneClass(s.change_pct)}`}>{fmtPct(s.change_pct)}</td>
                    <td className={`px-2 py-1.5 text-right ${toneClass(s.day_amt)}`}>{fmtYi(s.day_amt)}</td>
                    <td className={`whitespace-nowrap px-2 py-1.5 text-right ${toneClass(s.foreign_streak)}`}>
                      {streakLabel(s.foreign_streak)}
                    </td>
                    <td className={`px-3 py-1.5 text-right ${toneClass(s.sum20)}`}>{fmtYi(s.sum20)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </aside>
    </div>
  );
}
