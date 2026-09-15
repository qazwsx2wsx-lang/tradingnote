"""交易週誌：日誌、持股快照與每日持股漲跌的本地資料層。"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class HoldingMove:
    position_id: str
    ticker: str
    name: str
    shares: int
    close: float | None
    previous_close: float | None
    change_pct: float | None
    change_amount: float | None


@dataclass(frozen=True)
class JournalDay:
    date: str
    note: str
    holdings: tuple[HoldingMove, ...]
    total_change_amount: float | None
    market_open: bool
    holding_source: str
    snapshot_date: str | None


def _connect(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS journal_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS journal_entries (
            date TEXT PRIMARY KEY,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            date TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_snapshot_positions (
            snapshot_date TEXT NOT NULL,
            position_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            shares INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_date TEXT NOT NULL,
            market TEXT,
            PRIMARY KEY (snapshot_date, position_id),
            FOREIGN KEY (snapshot_date) REFERENCES portfolio_snapshots(date)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_snapshot_positions_date
            ON portfolio_snapshot_positions(snapshot_date);
        """
    )
    conn.commit()
    return conn


def _position_value(position, key, default=None):
    if isinstance(position, dict):
        return position.get(key, default)
    return getattr(position, key, default)


def initialize_journal(db_path, positions, today_iso=None):
    """建立附加資料表，首次啟用時記住日期，並保存當日完整持股。"""
    today_iso = today_iso or date.today().isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO journal_meta (key, value) VALUES ('started_on', ?)",
            (today_iso,),
        )
        _save_snapshot(conn, today_iso, positions)
        conn.commit()
        return conn.execute(
            "SELECT value FROM journal_meta WHERE key='started_on'"
        ).fetchone()[0]
    finally:
        conn.close()


def _save_snapshot(conn, snapshot_date, positions):
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO portfolio_snapshots (date, created_at, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET updated_at=excluded.updated_at""",
        (snapshot_date, now, now),
    )
    conn.execute(
        "DELETE FROM portfolio_snapshot_positions WHERE snapshot_date=?",
        (snapshot_date,),
    )
    rows = []
    for position in positions:
        rows.append(
            (
                snapshot_date,
                str(_position_value(position, "id", "")),
                str(_position_value(position, "ticker", "")).upper(),
                str(_position_value(position, "name", "") or ""),
                int(_position_value(position, "shares", 0)),
                float(_position_value(position, "entry_price", 0)),
                str(_position_value(position, "entry_date", "")),
                _position_value(position, "market"),
            )
        )
    if rows:
        conn.executemany(
            """INSERT INTO portfolio_snapshot_positions
               (snapshot_date, position_id, ticker, name, shares, entry_price,
                entry_date, market) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def save_portfolio_snapshot(db_path, snapshot_date, positions):
    conn = _connect(db_path)
    try:
        _save_snapshot(conn, snapshot_date, positions)
        conn.commit()
    finally:
        conn.close()


def save_journal_entry(db_path, entry_date, note):
    conn = _connect(db_path)
    try:
        note = note.rstrip()
        if not note:
            conn.execute("DELETE FROM journal_entries WHERE date=?", (entry_date,))
        else:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO journal_entries (date, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                       note=excluded.note, updated_at=excluded.updated_at""",
                (entry_date, note, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _position_dict(position):
    return {
        "position_id": str(_position_value(position, "id", "")),
        "ticker": str(_position_value(position, "ticker", "")).upper(),
        "name": str(_position_value(position, "name", "") or ""),
        "shares": int(_position_value(position, "shares", 0)),
        "entry_date": str(_position_value(position, "entry_date", "")),
    }


def load_week(db_path, week_start, current_positions, today_iso=None):
    """一次載入週一開始的七天日誌、持股狀態與每日價格變化。"""
    if isinstance(week_start, str):
        week_start = date.fromisoformat(week_start)
    today_iso = today_iso or date.today().isoformat()
    days = [(week_start + timedelta(days=offset)).isoformat() for offset in range(7)]
    start_iso, end_iso = days[0], days[-1]
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM journal_meta WHERE key='started_on'"
        ).fetchone()
        started_on = row[0] if row else today_iso
        notes = dict(
            conn.execute(
                "SELECT date, note FROM journal_entries WHERE date BETWEEN ? AND ?",
                (start_iso, end_iso),
            ).fetchall()
        )
        snapshot_dates = [
            row[0]
            for row in conn.execute(
                "SELECT date FROM portfolio_snapshots WHERE date <= ? ORDER BY date",
                (end_iso,),
            ).fetchall()
        ]
        snapshot_positions = {}
        for row in conn.execute(
            """SELECT snapshot_date, position_id, ticker, name, shares, entry_date
               FROM portfolio_snapshot_positions WHERE snapshot_date <= ?
               ORDER BY snapshot_date, position_id""",
            (end_iso,),
        ):
            snapshot_positions.setdefault(row[0], []).append(
                dict(
                    position_id=row[1], ticker=row[2], name=row[3] or "",
                    shares=row[4], entry_date=row[5]
                )
            )

        inferred = [_position_dict(position) for position in current_positions]
        holdings_by_day = {}
        source_by_day = {}
        all_tickers = set()
        for day in days:
            if day > today_iso:
                holdings, source, snapshot_date = [], "future", None
            elif day < started_on:
                holdings = [p for p in inferred if p["entry_date"] <= day]
                source, snapshot_date = "estimated", None
            else:
                eligible = [snapshot for snapshot in snapshot_dates if snapshot <= day]
                snapshot_date = eligible[-1] if eligible else None
                holdings = list(snapshot_positions.get(snapshot_date, [])) if snapshot_date else []
                source = "recorded"
            holdings_by_day[day] = holdings
            source_by_day[day] = (source, snapshot_date)
            all_tickers.update(p["ticker"] for p in holdings)

        has_price_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_prices'"
        ).fetchone() is not None
        prices = {}
        if all_tickers and has_price_table:
            placeholders = ",".join("?" for _ in all_tickers)
            query = (
                "SELECT ticker, date, close FROM daily_prices "
                f"WHERE ticker IN ({placeholders}) AND date <= ? AND close IS NOT NULL "
                "ORDER BY ticker, date"
            )
            for ticker, price_date, close in conn.execute(
                query, [*sorted(all_tickers), end_iso]
            ):
                prices.setdefault(ticker, []).append((price_date, float(close)))
        market_dates = (
            {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT date FROM daily_prices WHERE date BETWEEN ? AND ?",
                    (start_iso, end_iso),
                ).fetchall()
            }
            if has_price_table
            else set()
        )
    finally:
        conn.close()

    result = []
    for day in days:
        moves = []
        for holding in holdings_by_day[day]:
            history = prices.get(holding["ticker"], [])
            exact_index = next(
                (index for index, item in enumerate(history) if item[0] == day), None
            )
            close = previous = change_pct = change_amount = None
            if exact_index is not None:
                close = history[exact_index][1]
                if exact_index > 0:
                    previous = history[exact_index - 1][1]
                    if previous:
                        change_pct = (close - previous) / previous * 100
                        change_amount = holding["shares"] * (close - previous)
            moves.append(
                HoldingMove(
                    position_id=holding["position_id"],
                    ticker=holding["ticker"],
                    name=holding["name"],
                    shares=holding["shares"],
                    close=close,
                    previous_close=previous,
                    change_pct=change_pct,
                    change_amount=change_amount,
                )
            )
        available = [move.change_amount for move in moves if move.change_amount is not None]
        source, snapshot_date = source_by_day[day]
        result.append(
            JournalDay(
                date=day,
                note=notes.get(day, ""),
                holdings=tuple(moves),
                total_change_amount=sum(available) if available else None,
                market_open=day in market_dates,
                holding_source=source,
                snapshot_date=snapshot_date,
            )
        )
    return result
