"use client";

import dynamic from "next/dynamic";
import type { EChartsOption } from "echarts";

const ReactECharts = dynamic(() => import("echarts-for-react"), { ssr: false });

interface Props {
  option: EChartsOption;
  height: number;
  onClick?: (params: { name?: string; data?: unknown }) => void;
}

export default function EChart({ option, height, onClick }: Props) {
  return (
    <ReactECharts
      option={option}
      style={{ height, width: "100%" }}
      notMerge
      lazyUpdate
      onEvents={onClick ? { click: onClick } : undefined}
    />
  );
}
