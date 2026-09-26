import { getDb } from "./db";
import { buildSummaryText } from "./summaryText";
import type { HistoryPoint, Investor, SectorRow, StockRow, Summary } from "./types";
import { INVESTORS } from "./types";

export function isInvestor(value: string | null): value is Investor {
  return value !== null && (INVESTORS as string[]).includes(value);
}

/** 有預先計算指標的交易日，新到舊。 */
export function availableDates(): string[] {
  return getDb()
    .prepare("SELECT DISTINCT date FROM sector_metrics ORDER BY date DESC")
    .all()
    .map((r) => (r as { date: string }).date);
}

/** 使用者給的日期若不是交易日，退回到它之前最近一個有資料的交易日。 */
export function resolveDate(date: string | null): string | null {
  const row = date
    ? getDb().prepare("SELECT MAX(date) AS d FROM sector_metrics WHERE date <= ?").get(date)
    : getDb().prepare("SELECT MAX(date) AS d FROM sector_metrics").get();
  return (row as { d: string | null } | undefined)?.d ?? null;
}

export function getSectors(date: string, investor: Investor): SectorRow[] {
  return getDb()
    .prepare(
      `SELECT sector, day_amt, sum5, sum20, accel, streak, change5_pct, trading_value,
              stock_count, window_days
       FROM sector_metrics WHERE date = ? AND investor = ? ORDER BY day_amt DESC`,
    )
    .all(date, investor) as SectorRow[];
}

export function getSummary(date: string): Summary {
  const rows = getDb()
    .prepare("SELECT market, foreign_amt, trust_amt, dealer_amt FROM market_summary WHERE date = ?")
    .all(date) as { market: "TWSE" | "TPEX"; foreign_amt: number; trust_amt: number; dealer_amt: number }[];
  const byMarket = {
    TWSE: { foreign: 0, trust: 0, dealer: 0 },
    TPEX: { foreign: 0, trust: 0, dealer: 0 },
  };
  for (const r of rows) {
    byMarket[r.market] = {
      foreign: r.foreign_amt / 1e8,
      trust: r.trust_amt / 1e8,
      dealer: r.dealer_amt / 1e8,
    };
  }
  const foreign = byMarket.TWSE.foreign + byMarket.TPEX.foreign;
  const trust = byMarket.TWSE.trust + byMarket.TPEX.trust;
  const dealer = byMarket.TWSE.dealer + byMarket.TPEX.dealer;
  const text = buildSummaryText({
    foreign: getSectors(date, "foreign"),
    trust: getSectors(date, "trust"),
    dealer: getSectors(date, "dealer"),
  });
  return {
    date,
    amounts: { all: foreign + trust + dealer, foreign, trust, dealer },
    byMarket,
    text,
  };
}

export function getSectorHistory(
  sector: string,
  investor: Investor,
  days: number,
  date: string,
): HistoryPoint[] {
  const rows = getDb()
    .prepare(
      `SELECT date, day_amt FROM sector_metrics
       WHERE sector = ? AND investor = ? AND date <= ?
       ORDER BY date DESC LIMIT ?`,
    )
    .all(sector, investor, date, days) as HistoryPoint[];
  return rows.reverse();
}

export function getSectorStocks(sector: string, investor: Investor, date: string): StockRow[] {
  return getDb()
    .prepare(
      `SELECT m.ticker, m.name, m.market, p.close, p.change_pct,
              s.day_amt, s.sum20, COALESCE(f.streak, 0) AS foreign_streak
       FROM sector_map m
       JOIN stock_metrics s ON s.ticker = m.ticker AND s.date = ? AND s.investor = ?
       LEFT JOIN stock_metrics f ON f.ticker = m.ticker AND f.date = s.date AND f.investor = 'foreign'
       LEFT JOIN daily_prices p ON p.ticker = m.ticker AND p.date = s.date
       WHERE m.sector = ?
       ORDER BY s.day_amt DESC`,
    )
    .all(date, investor, sector) as StockRow[];
}
