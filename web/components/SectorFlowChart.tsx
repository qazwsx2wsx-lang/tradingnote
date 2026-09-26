"use client";

import { useEffect, useState } from "react";
import type { EChartsOption } from "echarts";
import EChart from "./EChart";
import { usePalette } from "@/lib/useTheme";
import type { HistoryPoint, Investor } from "@/lib/types";

/** 類股近 N 日每日資金流（柱）＋可選累計線 */
export default function SectorFlowChart({
  sector,
  investor,
  date,
  days = 30,
  height = 240,
}: {
  sector: string;
  investor: Investor;
  date: string;
  days?: number;
  height?: number;
}) {
  const p = usePalette();
  const [points, setPoints] = useState<HistoryPoint[] | null>(null);
  const [cumulative, setCumulative] = useState(true);

  useEffect(() => {
    let alive = true;
    setPoints(null);
    fetch(`/api/sectors/${encodeURIComponent(sector)}/history?days=${days}&investor=${investor}&date=${date}`)
      .then((r) => r.json())
      .then((d) => alive && setPoints(d.history ?? []));
    return () => {
      alive = false;
    };
  }, [sector, investor, date, days]);

  if (!points) return <div className="muted py-10 text-center text-sm">載入中…</div>;
  if (!points.length) return <div className="muted py-10 text-center text-sm">沒有資料</div>;

  let running = 0;
  const cum = points.map((pt) => +(running += pt.day_amt).toFixed(2));
  const option: EChartsOption = {
    grid: { left: 44, right: cumulative ? 44 : 12, top: 24, bottom: 28 },
    tooltip: { trigger: "axis", valueFormatter: (v) => `${Number(v).toFixed(2)} 億` },
    xAxis: {
      type: "category",
      data: points.map((pt) => pt.date.slice(5)),
      axisLabel: { color: p.muted, fontSize: 10 },
      axisLine: { lineStyle: { color: p.grid } },
    },
    yAxis: [
      { type: "value", name: "億/日", nameTextStyle: { color: p.muted, fontSize: 10 },
        axisLabel: { color: p.muted, fontSize: 10 }, splitLine: { lineStyle: { color: p.grid } } },
      { type: "value", show: cumulative, name: "累計", nameTextStyle: { color: p.muted, fontSize: 10 },
        axisLabel: { color: p.muted, fontSize: 10 }, splitLine: { show: false } },
    ],
    series: [
      {
        name: "當日",
        type: "bar",
        data: points.map((pt) => ({
          value: +pt.day_amt.toFixed(2),
          itemStyle: { color: pt.day_amt >= 0 ? p.gain : p.loss },
        })),
      },
      ...(cumulative
        ? [{ name: "累計", type: "line" as const, yAxisIndex: 1, data: cum, symbol: "none",
             lineStyle: { color: p.foreign, width: 2 }, itemStyle: { color: p.foreign } }]
        : []),
    ],
  };

  return (
    <div>
      <label className="muted mb-1 flex items-center gap-1.5 text-xs">
        <input type="checkbox" checked={cumulative} onChange={(e) => setCumulative(e.target.checked)} />
        疊加累計線
      </label>
      <EChart option={option} height={height} />
    </div>
  );
}
