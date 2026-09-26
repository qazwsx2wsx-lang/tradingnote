export type Investor = "all" | "foreign" | "trust" | "dealer";

export const INVESTORS: Investor[] = ["all", "foreign", "trust", "dealer"];

export const INVESTOR_LABEL: Record<Investor, string> = {
  all: "合計",
  foreign: "外資",
  trust: "投信",
  dealer: "自營",
};

export interface SectorRow {
  sector: string;
  day_amt: number;
  sum5: number;
  sum20: number;
  accel: number | null;
  streak: number;
  change5_pct: number | null;
  trading_value: number;
  stock_count: number;
  window_days: number;
}

export interface Summary {
  date: string;
  amounts: Record<Investor, number>; // 億，交易所公布（含 ETF），上市＋上櫃
  byMarket: Record<"TWSE" | "TPEX", Record<Exclude<Investor, "all">, number>>;
  text: string;
}

export interface HistoryPoint {
  date: string;
  day_amt: number;
}

export interface StockRow {
  ticker: string;
  name: string;
  market: string;
  close: number | null;
  change_pct: number | null;
  day_amt: number;
  foreign_streak: number;
  sum20: number;
}
