"use client";

import { useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import EChart from "./EChart";
import InvestorTabs from "./InvestorTabs";
import Section from "./Section";
import SectorFlowChart from "./SectorFlowChart";
import { fmtYi } from "@/lib/format";
import { usePalette } from "@/lib/useTheme";
import type { Investor, SectorRow } from "@/lib/types";

type SizeMetric = "value" | "sum20";

export default function BubbleChart({
  sectorsBy,
  date,
  onSelect,
}: {
  sectorsBy: Record<Investor, SectorRow[]> | null;
  date: string;
  onSelect: (sector: string) => void;
}) {
  const p = usePalette();
  const [investor, setInvestor] = useState<Investor>("all");
  const [sizeMetric, setSizeMetric] = useState<SizeMetric>("value");
  const rows = useMemo(() => sectorsBy?.[investor] ?? [], [sectorsBy, investor]);
  const defaultTrend = useMemo(
    () => [...rows].sort((a, b) => Math.abs(b.sum5) - Math.abs(a.sum5))[0]?.sector ?? "",
    [rows],
  );
  const [trendSector, setTrendSector] = useState("");
  const trend = rows.some((r) => r.sector === trendSector) ? trendSector : defaultTrend;

  const option = useMemo<EChartsOption>(() => {
    const sizeOf = (r: SectorRow) => (sizeMetric === "value" ? r.trading_value : Math.abs(r.sum20));
    const maxSize = Math.max(1e-9, ...rows.map(sizeOf));
    return {
      grid: { left: 48, right: 16, top: 40, bottom: 44 },
      dataZoom: [
        // 只在按住 Ctrl（或觸控板雙指捏合）時縮放／拖曳，避免捲動頁面時被圖表攔截
        ...[{ xAxisIndex: 0 }, { yAxisIndex: 0 }].map((axis) => ({
          type: "inside" as const, filterMode: "none" as const, ...axis,
          zoomOnMouseWheel: "ctrl" as const, moveOnMouseMove: "ctrl" as const, moveOnMouseWheel: false,
        })),
      ],
      tooltip: {
        formatter: (params) => {
          const r = (params as unknown as { data: { row: SectorRow } }).data.row;
          return [
            `<b>${r.sector}</b>（${r.stock_count} 檔）`,
            `近5日：${fmtYi(r.sum5)} 億`,
            `加速流入：${fmtYi(r.accel)} 億/天`,
            `近20日：${fmtYi(r.sum20)} 億`,
            `當日成交：${r.trading_value.toFixed(0)} 億`,
          ].join("<br/>");
        },
      },
      xAxis: {
        type: "value", name: "近5日買賣超（億）", nameLocation: "middle", nameGap: 28,
        nameTextStyle: { color: p.muted, fontSize: 11 }, axisLabel: { color: p.muted, fontSize: 10 },
        splitLine: { lineStyle: { color: p.grid } },
      },
      yAxis: {
        type: "value", name: "加速流入（億/天）", nameTextStyle: { color: p.muted, fontSize: 11 },
        axisLabel: { color: p.muted, fontSize: 10 }, splitLine: { lineStyle: { color: p.grid } },
      },
      series: [
        {
          type: "scatter",
          data: rows.map((r) => ({
            name: r.sector,
            value: [r.sum5, r.accel ?? 0],
            row: r,
            symbolSize: 10 + 46 * Math.sqrt(sizeOf(r) / maxSize),
            itemStyle: { color: r.sum5 >= 0 ? p.gainSoft : p.lossSoft, borderColor: r.sum5 >= 0 ? p.gain : p.loss },
          })),
          label: { show: true, formatter: "{b}", color: p.text, fontSize: 10, position: "top" },
          labelLayout: { hideOverlap: true },
          emphasis: { focus: "self", label: { fontWeight: "bold" } },
          markLine: {
            silent: true, symbol: "none", lineStyle: { color: p.muted, type: "dashed" }, label: { show: false },
            data: [{ xAxis: 0 }, { yAxis: 0 }],
          },
        },
      ],
    } as EChartsOption;
  }, [rows, sizeMetric, p]);

  return (
    <Section
      title="法人資金流向泡泡圖"
      subtitle="右上＝近 5 日買超且正在加速；點泡泡看類股明細"
      actions={<InvestorTabs value={investor} onChange={setInvestor} />}
    >
      <div className="muted mb-1 flex items-center gap-2 text-xs">
        泡泡大小：
        <select
          value={sizeMetric}
          onChange={(e) => setSizeMetric(e.target.value as SizeMetric)}
          className="rounded border border-[var(--border)] bg-transparent px-1 py-0.5"
        >
          <option value="value">當日成交金額</option>
          <option value="sum20">近 20 日買賣超絕對值</option>
        </select>
      </div>
      <EChart option={option} height={380} onClick={(e) => e.name && onSelect(e.name)} />
      <p className="muted mt-1 text-xs">加速流入＝近 5 天比前 5 天，平均每天多買多少（億／天）。Ctrl＋滾輪或觸控板雙指可縮放。</p>
      <details className="mt-3 rounded-lg border border-[var(--border)] p-3 [&_summary::-webkit-details-marker]:hidden">
        <summary className="cursor-pointer list-none select-none text-sm font-medium">
          <span className="inline-block transition-transform [details[open]_&]:rotate-90">▶</span> 過去 30 天
        </summary>
        <div className="mt-2">
          <select
            value={trend}
            onChange={(e) => setTrendSector(e.target.value)}
            className="mb-2 rounded border border-[var(--border)] bg-transparent px-2 py-1 text-sm"
          >
            {rows.map((r) => (
              <option key={r.sector} value={r.sector}>{r.sector}</option>
            ))}
          </select>
          {trend && <SectorFlowChart sector={trend} investor={investor} date={date} />}
        </div>
      </details>
    </Section>
  );
}
