import type { Investor, SectorRow } from "./types";

const BIG = 20; // 億：超過這個金額用「大買／大賣」

function verb(amount: number): string {
  if (amount >= BIG) return "大買";
  if (amount > 0) return "加碼";
  if (amount <= -BIG) return "大賣";
  return "調節";
}

/** 依各法人當日買賣超金額絕對值最大的類股，組一句盤後摘要，例如「外資大買半導體業，投信加碼航運業」。 */
export function buildSummaryText(
  byInvestor: Partial<Record<Exclude<Investor, "all">, SectorRow[]>>,
): string {
  const labels: [Exclude<Investor, "all">, string][] = [
    ["foreign", "外資"],
    ["trust", "投信"],
    ["dealer", "自營商"],
  ];
  // 同一個動作＋類股合併成一句，例如「外資、投信大賣半導體業」
  const groups = new Map<string, string[]>();
  for (const [inv, label] of labels) {
    const rows = byInvestor[inv] ?? [];
    const top = rows.reduce<SectorRow | null>(
      (best, r) => (best === null || Math.abs(r.day_amt) > Math.abs(best.day_amt) ? r : best),
      null,
    );
    if (!top || Math.abs(top.day_amt) < 0.5) continue;
    const key = `${verb(top.day_amt)}${top.sector}`;
    groups.set(key, [...(groups.get(key) ?? []), label]);
  }
  const parts = [...groups].map(([action, who]) => `${who.join("、")}${action}`);
  return parts.length ? parts.join("，") : "三大法人今日類股進出不明顯";
}
