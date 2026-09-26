"use client";

import { useEffect, useState } from "react";

export interface ChartPalette {
  dark: boolean;
  gain: string;
  loss: string;
  gainSoft: string;
  lossSoft: string;
  text: string;
  muted: string;
  grid: string;
  foreign: string;
  trust: string;
}

const LIGHT: ChartPalette = {
  dark: false, gain: "#dc2626", loss: "#15803d", gainSoft: "rgba(220,38,38,0.55)",
  lossSoft: "rgba(21,128,61,0.55)", text: "#15181e", muted: "#5f6775", grid: "#e3e6eb",
  foreign: "#2563eb", trust: "#d97706",
};
const DARK: ChartPalette = {
  dark: true, gain: "#ef4444", loss: "#22c55e", gainSoft: "rgba(239,68,68,0.55)",
  lossSoft: "rgba(34,197,94,0.5)", text: "#f1f3f5", muted: "#9da5b4", grid: "#292e38",
  foreign: "#5b9df9", trust: "#f59e0b",
};

export function usePalette(): ChartPalette {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    setDark(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setDark(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return dark ? DARK : LIGHT;
}
