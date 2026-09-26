"use client";

import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart from "./EChart";
import Section from "./Section";
import { fmtYi } from "@/lib/format";
import { usePalette } from "@/lib/useTheme";
import type { Investor, SectorRow } from "@/lib/types";

type Tag = "土洋同買" | "土洋同賣" | "土洋對作";

export function tuYangTag(foreign: number, trust: number): Tag | null {
  if (Math.abs(foreign) < 0.05 || Math.abs(trust) < 0.05) return null;
  if (foreign > 0 && trust > 0) return "土洋同買";
  if (foreign < 0 && trust < 0) return "土洋同賣";
  return "土洋對作";
}

export default function TuYangChart({
  sectorsBy,
  onSelect,
}: {
  sectorsBy: Record<Investor, SectorRow[]> | null;
  onSelect: (sector: string) => void;
}) {
  const p = usePalette();
  const rows = useMemo(() => {
    const trust = new Map((sectorsBy?.trust ?? []).map((r) => [r.sector, r.sum5]));
    return (sectorsBy?.foreign ?? [])
      .map((r) => {
        const t = trust.get(r.sector) ?? 0;
        return { sector: r.sector, foreign: r.sum5, trust: t, tag: tuYangTag(r.sum5, t) };
      })
      .sort((a, b) => Math.abs(b.foreign) + Math.abs(b.trust) - (Math.abs(a.foreign) + Math.abs(a.trust)))
      .slice(0, 12);
  }, [sectorsBy]);

  const tagColor = (tag: Tag | null) =>
    tag === "土洋同買" ? p.gain : tag === "土洋同賣" ? p.loss : tag === "土洋對作" ? "#a78bfa" : p.muted;

  const option: EChartsOption = {
    grid: { left: 8, right: 8, top: 30, bottom: 8, containLabel: true },
    legend: { data: ["外資", "投信"], textStyle: { color: p.muted }, top: 0 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => `${fmtYi(Number(v))} 億` },
    xAxis: {
      type: "category",
      data: rows.map((r) => r.sector),
      axisLabel: {
        interval: 0, rotate: 40, fontSize: 10,
        formatter: (name: string) => {
          const tag = rows.find((r) => r.sector === name)?.tag;
          return tag ? `{${tag === "土洋同買" ? "buy" : tag === "土洋同賣" ? "sell" : "vs"}|●} ${name}` : name;
        },
        rich: { buy: { color: tagColor("土洋同買") }, sell: { color: tagColor("土洋同賣") }, vs: { color: tagColor("土洋對作") } },
        color: p.text,
      },
    },
    yAxis: { type: "value", name: "億", axisLabel: { color: p.muted, fontSize: 10 }, splitLine: { lineStyle: { color: p.grid } } },
    series: [
      { name: "外資", type: "bar", data: rows.map((r) => +r.foreign.toFixed(2)), itemStyle: { color: p.foreign } },
      { name: "投信", type: "bar", data: rows.map((r) => +r.trust.toFixed(2)), itemStyle: { color: p.trust } },
    ],
  };

  const tagged = rows.filter((r) => r.tag === "土洋同買" || r.tag === "土洋對作");
  return (
    <Section title="近 5 日土洋操作" subtitle="外資（洋）與投信（土）近 5 日類股買賣超比較">
      <EChart option={option} height={320} onClick={(e) => e.name && onSelect(e.name)} />
      <div className="mt-2 flex flex-wrap gap-1.5">
        {tagged.map((r) => (
          <button
            key={r.sector}
            onClick={() => onSelect(r.sector)}
            className="rounded-full border px-2 py-0.5 text-xs"
            style={{ borderColor: tagColor(r.tag), color: tagColor(r.tag) }}
          >
            {r.tag}・{r.sector}
          </button>
        ))}
      </div>
    </Section>
  );
}
