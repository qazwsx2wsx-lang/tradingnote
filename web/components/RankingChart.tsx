"use client";

import { useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import EChart from "./EChart";
import InvestorTabs from "./InvestorTabs";
import Section from "./Section";
import { usePalette, type ChartPalette } from "@/lib/useTheme";
import type { Investor, SectorRow } from "@/lib/types";

function barOption(rows: SectorRow[], p: ChartPalette, buy: boolean): EChartsOption {
  const ordered = [...rows].reverse(); // 水平長條圖由下往上畫，第一名放最上面
  return {
    grid: { left: 8, right: 44, top: 4, bottom: 4, containLabel: true },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => `${Number(v).toFixed(2)} 億` },
    xAxis: { type: "value", show: false },
    yAxis: {
      type: "category", data: ordered.map((r) => r.sector), position: buy ? "left" : "right",
      axisLabel: { color: p.text, fontSize: 11 }, axisLine: { show: false }, axisTick: { show: false },
    },
    series: [{
      type: "bar", barMaxWidth: 16,
      data: ordered.map((r) => +r.day_amt.toFixed(2)),
      itemStyle: { color: buy ? p.gain : p.loss, borderRadius: 3 },
      label: { show: true, position: buy ? "right" : "left", color: p.muted, fontSize: 10,
               formatter: (x) => Number(x.value).toFixed(1) },
    }],
  };
}

export default function RankingChart({ sectorsBy }: { sectorsBy: Record<Investor, SectorRow[]> | null }) {
  const p = usePalette();
  const [investor, setInvestor] = useState<Investor>("all");
  const rows = useMemo(() => sectorsBy?.[investor] ?? [], [sectorsBy, investor]);
  const buys = rows.filter((r) => r.day_amt > 0).sort((a, b) => b.day_amt - a.day_amt).slice(0, 10);
  const sells = rows.filter((r) => r.day_amt < 0).sort((a, b) => a.day_amt - b.day_amt).slice(0, 10);
  const height = (n: number) => Math.max(80, n * 26 + 10);

  return (
    <Section title="今日法人買賣榜" subtitle="類股當日買賣超，單位：億元" actions={<InvestorTabs value={investor} onChange={setInvestor} />}>
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="gain mb-1 text-sm font-semibold">買超前 10</h3>
          {buys.length ? <EChart option={barOption(buys, p, true)} height={height(buys.length)} /> : <p className="muted text-sm">無</p>}
        </div>
        <div>
          <h3 className="loss mb-1 text-sm font-semibold">賣超前 10</h3>
          {sells.length ? <EChart option={barOption(sells, p, false)} height={height(sells.length)} /> : <p className="muted text-sm">無</p>}
        </div>
      </div>
    </Section>
  );
}
