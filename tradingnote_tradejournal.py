"""交易日誌（策略／標籤／逐筆成交／復盤診斷）的本地資料層。

部位（Position）＝目前還 OPEN 的交易：trade_as_position／list_positions／
list_open_positions 把 trades 資料表映射成既有的 Position view-model，讓
tradingnote_journal.py／compute_pnl 等既有程式碼完全不用改；open_position／
update_position_plan／cancel_empty_position／migrate_legacy_position 則是
GUI／CLI 操作部位時實際寫入 trades 的入口，取代舊版 positions.json 的
add_position／update_position／remove_position。"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sqlite3

from tradingnote_core import Position, lookup_price, validate_position_input


@dataclass(frozen=True)
class Strategy:
    strategy_id: int
    name: str
    description: str | None
    created_at: str


@dataclass(frozen=True)
class Tag:
    tag_id: int
    name: str
    category: str | None


@dataclass(frozen=True)
class TradeExecution:
    execution_id: int
    trade_id: int
    action: str
    price: float
    qty: float
    commission: float
    slippage: float
    executed_at: str


@dataclass(frozen=True)
class TradeAnalytics:
    trade_id: int
    mae_price: float | None
    mfe_price: float | None
    mae_amount: float | None
    mfe_amount: float | None
    mae_r: float | None
    mfe_r: float | None
    realized_r: float | None
    holding_duration_seconds: int | None
    updated_at: str


@dataclass(frozen=True)
class Trade:
    trade_id: int
    symbol: str
    side: str
    strategy_id: int | None
    status: str
    initial_entry_price: float | None
    initial_stop_loss: float
    initial_take_profit: float | None
    planned_risk_amount: float | None
    total_qty: float
    avg_entry_price: float | None
    avg_exit_price: float | None
    realized_pnl: float
    total_commission: float
    net_pnl: float
    execution_rating: int | None
    notes: str | None
    chart_screenshot_url: str | None
    opened_at: str
    closed_at: str | None
    created_at: str
    executions: tuple[TradeExecution, ...] = field(default_factory=tuple)
    tags: tuple[Tag, ...] = field(default_factory=tuple)
    analytics: TradeAnalytics | None = None


def _connect(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategies (
            strategy_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tags (
            tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT CHECK(category IN ('EMOTION', 'EXECUTION', 'MARKET_CONDITION'))
        );

        CREATE TABLE IF NOT EXISTS trades (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT CHECK(side IN ('LONG', 'SHORT')) NOT NULL,
            strategy_id INTEGER,
            status TEXT CHECK(status IN ('OPEN', 'CLOSED', 'CANCELLED')) DEFAULT 'OPEN',

            initial_entry_price REAL,
            initial_stop_loss REAL NOT NULL,
            initial_take_profit REAL,
            planned_risk_amount REAL,

            total_qty REAL DEFAULT 0,
            avg_entry_price REAL,
            avg_exit_price REAL,
            realized_pnl REAL DEFAULT 0,
            total_commission REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,

            execution_rating INTEGER CHECK(execution_rating BETWEEN 1 AND 5),
            notes TEXT,
            chart_screenshot_url TEXT,

            opened_at DATETIME NOT NULL,
            closed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
        );

        CREATE TABLE IF NOT EXISTS trade_executions (
            execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            action TEXT CHECK(action IN ('BUY', 'SELL')) NOT NULL,
            price REAL NOT NULL,
            qty REAL NOT NULL,
            commission REAL DEFAULT 0,
            slippage REAL DEFAULT 0,
            executed_at DATETIME NOT NULL,
            FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS trade_tags (
            trade_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (trade_id, tag_id),
            FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS trade_analytics (
            trade_id INTEGER PRIMARY KEY,

            mae_price REAL,
            mfe_price REAL,
            mae_amount REAL,
            mfe_amount REAL,
            mae_r REAL,
            mfe_r REAL,

            realized_r REAL,
            holding_duration_seconds INTEGER,

            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON trades(opened_at);
        CREATE INDEX IF NOT EXISTS idx_executions_trade_id ON trade_executions(trade_id);
        """
    )
    conn.commit()
    return conn


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def create_strategy(db_path, name, description=None):
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO strategies (name, description) VALUES (?, ?)",
            (name, description),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_strategies(db_path):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT strategy_id, name, description, created_at FROM strategies "
            "ORDER BY strategy_id"
        ).fetchall()
        return [Strategy(*row) for row in rows]
    finally:
        conn.close()


def get_strategy(db_path, strategy_id):
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT strategy_id, name, description, created_at FROM strategies "
            "WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()
        return Strategy(*row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def create_tag(db_path, name, category=None):
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO tags (name, category) VALUES (?, ?)",
            (name, category),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_tags(db_path, category=None):
    conn = _connect(db_path)
    try:
        if category is None:
            rows = conn.execute(
                "SELECT tag_id, name, category FROM tags ORDER BY tag_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tag_id, name, category FROM tags WHERE category=? "
                "ORDER BY tag_id",
                (category,),
            ).fetchall()
        return [Tag(*row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trade lifecycle
# ---------------------------------------------------------------------------


def open_trade(
    db_path,
    *,
    symbol,
    side,
    initial_stop_loss,
    strategy_id=None,
    initial_entry_price=None,
    initial_take_profit=None,
    planned_risk_amount=None,
    opened_at=None,
    notes=None,
    chart_screenshot_url=None,
):
    opened_at = opened_at or _now_iso()
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO trades (
                   symbol, side, strategy_id, status,
                   initial_entry_price, initial_stop_loss, initial_take_profit,
                   planned_risk_amount, total_qty, realized_pnl,
                   total_commission, net_pnl, notes, chart_screenshot_url,
                   opened_at
               ) VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)""",
            (
                symbol,
                side,
                strategy_id,
                initial_entry_price,
                initial_stop_loss,
                initial_take_profit,
                planned_risk_amount,
                notes,
                chart_screenshot_url,
                opened_at,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _recalculate_rollup(conn, trade_id):
    row = conn.execute(
        "SELECT side, status FROM trades WHERE trade_id=?", (trade_id,)
    ).fetchone()
    if row is None:
        return
    side, status = row

    rows = conn.execute(
        "SELECT action, price, qty, commission FROM trade_executions "
        "WHERE trade_id=? ORDER BY executed_at, execution_id",
        (trade_id,),
    ).fetchall()

    entry_action = "BUY" if side == "LONG" else "SELL"

    open_qty = 0.0
    entry_cost_sum = 0.0
    entry_qty_sum = 0.0
    exit_proceeds_sum = 0.0
    exit_qty_sum = 0.0
    realized_pnl = 0.0
    total_commission = 0.0

    for action, price, qty, commission in rows:
        total_commission += commission
        if action == entry_action:
            entry_cost_sum += price * qty
            entry_qty_sum += qty
            open_qty += qty
        else:
            exit_proceeds_sum += price * qty
            exit_qty_sum += qty
            running_avg_entry = entry_cost_sum / entry_qty_sum if entry_qty_sum else 0.0
            if side == "LONG":
                realized_pnl += (price - running_avg_entry) * qty
            else:
                realized_pnl += (running_avg_entry - price) * qty
            open_qty -= qty

    avg_entry_price = (entry_cost_sum / entry_qty_sum) if entry_qty_sum else None
    avg_exit_price = (exit_proceeds_sum / exit_qty_sum) if exit_qty_sum else None
    net_pnl = realized_pnl - total_commission
    total_qty = round(open_qty, 10)

    new_status = status
    newly_closed = False
    if status == "OPEN" and entry_qty_sum > 0 and total_qty <= 0:
        new_status = "CLOSED"
        newly_closed = True

    if newly_closed:
        conn.execute(
            """UPDATE trades SET
                   total_qty=?, avg_entry_price=?, avg_exit_price=?,
                   realized_pnl=?, total_commission=?, net_pnl=?, status=?,
                   closed_at=CURRENT_TIMESTAMP
               WHERE trade_id=?""",
            (
                total_qty,
                avg_entry_price,
                avg_exit_price,
                realized_pnl,
                total_commission,
                net_pnl,
                new_status,
                trade_id,
            ),
        )
    else:
        conn.execute(
            """UPDATE trades SET
                   total_qty=?, avg_entry_price=?, avg_exit_price=?,
                   realized_pnl=?, total_commission=?, net_pnl=?, status=?
               WHERE trade_id=?""",
            (
                total_qty,
                avg_entry_price,
                avg_exit_price,
                realized_pnl,
                total_commission,
                net_pnl,
                new_status,
                trade_id,
            ),
        )


def recalculate_trade_rollup(db_path, trade_id):
    conn = _connect(db_path)
    try:
        _recalculate_rollup(conn, trade_id)
        conn.commit()
    finally:
        conn.close()


def record_execution(
    db_path,
    trade_id,
    *,
    action,
    price,
    qty,
    commission=0.0,
    slippage=0.0,
    executed_at=None,
):
    executed_at = executed_at or _now_iso()
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO trade_executions
                   (trade_id, action, price, qty, commission, slippage, executed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, action, price, qty, commission, slippage, executed_at),
        )
        execution_id = cursor.lastrowid
        _recalculate_rollup(conn, trade_id)
        conn.commit()
        return execution_id
    finally:
        conn.close()


def close_trade(db_path, trade_id, *, closed_at=None, execution_rating=None):
    closed_at = closed_at or _now_iso()
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE trades SET status='CLOSED', closed_at=?, "
            "execution_rating=COALESCE(?, execution_rating) WHERE trade_id=?",
            (closed_at, execution_rating, trade_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_trade_notes(db_path, trade_id, *, notes=None, execution_rating=None):
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE trades SET notes=?, execution_rating=? WHERE trade_id=?",
            (notes, execution_rating, trade_id),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_trade(db_path, trade_id):
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE trades SET status='CANCELLED' WHERE trade_id=?", (trade_id,)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tags on trades
# ---------------------------------------------------------------------------


def attach_tag(db_path, trade_id, tag_id):
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO trade_tags (trade_id, tag_id) VALUES (?, ?)",
            (trade_id, tag_id),
        )
        conn.commit()
    finally:
        conn.close()


def detach_tag(db_path, trade_id, tag_id):
    conn = _connect(db_path)
    try:
        conn.execute(
            "DELETE FROM trade_tags WHERE trade_id=? AND tag_id=?",
            (trade_id, tag_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def upsert_trade_analytics(
    db_path,
    trade_id,
    *,
    mae_price=None,
    mfe_price=None,
    mae_amount=None,
    mfe_amount=None,
    mae_r=None,
    mfe_r=None,
    realized_r=None,
    holding_duration_seconds=None,
):
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT INTO trade_analytics (
                   trade_id, mae_price, mfe_price, mae_amount, mfe_amount,
                   mae_r, mfe_r, realized_r, holding_duration_seconds
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_id) DO UPDATE SET
                   mae_price=excluded.mae_price,
                   mfe_price=excluded.mfe_price,
                   mae_amount=excluded.mae_amount,
                   mfe_amount=excluded.mfe_amount,
                   mae_r=excluded.mae_r,
                   mfe_r=excluded.mfe_r,
                   realized_r=excluded.realized_r,
                   holding_duration_seconds=excluded.holding_duration_seconds,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                trade_id,
                mae_price,
                mfe_price,
                mae_amount,
                mfe_amount,
                mae_r,
                mfe_r,
                realized_r,
                holding_duration_seconds,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_TRADE_COLUMNS = (
    "trade_id, symbol, side, strategy_id, status, initial_entry_price, "
    "initial_stop_loss, initial_take_profit, planned_risk_amount, total_qty, "
    "avg_entry_price, avg_exit_price, realized_pnl, total_commission, net_pnl, "
    "execution_rating, notes, chart_screenshot_url, opened_at, closed_at, created_at"
)


def _row_to_trade(row, executions=(), tags=(), analytics=None):
    return Trade(
        trade_id=row[0],
        symbol=row[1],
        side=row[2],
        strategy_id=row[3],
        status=row[4],
        initial_entry_price=row[5],
        initial_stop_loss=row[6],
        initial_take_profit=row[7],
        planned_risk_amount=row[8],
        total_qty=row[9],
        avg_entry_price=row[10],
        avg_exit_price=row[11],
        realized_pnl=row[12],
        total_commission=row[13],
        net_pnl=row[14],
        execution_rating=row[15],
        notes=row[16],
        chart_screenshot_url=row[17],
        opened_at=row[18],
        closed_at=row[19],
        created_at=row[20],
        executions=executions,
        tags=tags,
        analytics=analytics,
    )


def get_trade(db_path, trade_id):
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {_TRADE_COLUMNS} FROM trades WHERE trade_id=?", (trade_id,)
        ).fetchone()
        if row is None:
            return None

        execution_rows = conn.execute(
            "SELECT execution_id, trade_id, action, price, qty, commission, "
            "slippage, executed_at FROM trade_executions WHERE trade_id=? "
            "ORDER BY executed_at, execution_id",
            (trade_id,),
        ).fetchall()
        executions = tuple(TradeExecution(*r) for r in execution_rows)

        tag_rows = conn.execute(
            "SELECT t.tag_id, t.name, t.category FROM tags t "
            "JOIN trade_tags tt ON tt.tag_id = t.tag_id "
            "WHERE tt.trade_id=? ORDER BY t.tag_id",
            (trade_id,),
        ).fetchall()
        tags = tuple(Tag(*r) for r in tag_rows)

        analytics_row = conn.execute(
            "SELECT trade_id, mae_price, mfe_price, mae_amount, mfe_amount, "
            "mae_r, mfe_r, realized_r, holding_duration_seconds, updated_at "
            "FROM trade_analytics WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
        analytics = TradeAnalytics(*analytics_row) if analytics_row else None

        return _row_to_trade(row, executions=executions, tags=tags, analytics=analytics)
    finally:
        conn.close()


def list_trades(
    db_path,
    *,
    symbol=None,
    status=None,
    strategy_id=None,
    date_from=None,
    date_to=None,
):
    conn = _connect(db_path)
    try:
        clauses = []
        params = []
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if strategy_id is not None:
            clauses.append("strategy_id=?")
            params.append(strategy_id)
        if date_from is not None:
            clauses.append("opened_at >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("opened_at <= ?")
            params.append(date_to)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT {_TRADE_COLUMNS} FROM trades {where} ORDER BY opened_at DESC",
            params,
        ).fetchall()
        return [_row_to_trade(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 部位（Position）view-model：把 trades 映射成既有的 Position dataclass
# ---------------------------------------------------------------------------


def trade_as_position(trade, snapshot=None):
    name, market = "", None
    if snapshot is not None:
        price = lookup_price(trade.symbol, snapshot)
        if price is not None:
            name, market = price.name, price.market
    shares = trade.total_qty
    return Position(
        ticker=trade.symbol,
        shares=int(shares) if float(shares).is_integer() else shares,
        entry_price=trade.avg_entry_price or 0.0,
        entry_date=trade.opened_at[:10],
        note=trade.notes or "",
        name=name,
        market=market,
        id=str(trade.trade_id),
        updated_at=trade.opened_at[:10],
    )


def list_positions(db_path, *, statuses=("OPEN",), snapshot=None):
    if statuses is None:
        trades = list_trades(db_path)
    else:
        trades = sorted(
            (t for status in statuses for t in list_trades(db_path, status=status)),
            key=lambda t: t.opened_at,
            reverse=True,
        )
    return [trade_as_position(t, snapshot) for t in trades]


def list_open_positions(db_path, snapshot=None):
    return list_positions(db_path, statuses=("OPEN",), snapshot=snapshot)


def open_position(
    db_path,
    *,
    ticker,
    shares,
    entry_price,
    entry_date,
    initial_stop_loss,
    side="LONG",
    note="",
    strategy_id=None,
    initial_take_profit=None,
    planned_risk_amount=None,
    snapshot=None,
):
    ticker = ticker.strip().upper()
    validate_position_input(ticker, shares, entry_price, entry_date)
    trade_id = open_trade(
        db_path,
        symbol=ticker,
        side=side,
        initial_stop_loss=initial_stop_loss,
        strategy_id=strategy_id,
        initial_entry_price=entry_price,
        initial_take_profit=initial_take_profit,
        planned_risk_amount=planned_risk_amount,
        opened_at=entry_date,
        notes=note or None,
    )
    record_execution(
        db_path,
        trade_id,
        action="BUY" if side == "LONG" else "SELL",
        price=entry_price,
        qty=shares,
        executed_at=entry_date,
    )
    return trade_as_position(get_trade(db_path, trade_id), snapshot)


_UNSET = object()


def update_position_plan(
    db_path,
    trade_id,
    *,
    strategy_id=_UNSET,
    initial_stop_loss=_UNSET,
    initial_take_profit=_UNSET,
    planned_risk_amount=_UNSET,
    notes=_UNSET,
):
    """只改「計畫」欄位（策略／停損／停利／風險金額／備註），股數與均價一律要
    透過 record_execution 調整。用 _UNSET 而不是「None 就跳過」，才能明確表達
    「清空停利價」而不是「不要改停利價」。initial_stop_loss 本身是 NOT NULL
    欄位，傳 None 進來直接視為呼叫端寫錯，丟 ValueError。"""
    fields = {}
    if strategy_id is not _UNSET:
        fields["strategy_id"] = strategy_id
    if initial_stop_loss is not _UNSET:
        if initial_stop_loss is None:
            raise ValueError("停損價不可為空。")
        fields["initial_stop_loss"] = initial_stop_loss
    if initial_take_profit is not _UNSET:
        fields["initial_take_profit"] = initial_take_profit
    if planned_risk_amount is not _UNSET:
        fields["planned_risk_amount"] = planned_risk_amount
    if notes is not _UNSET:
        fields["notes"] = notes
    if not fields:
        return

    conn = _connect(db_path)
    try:
        set_clause = ", ".join(f"{column}=?" for column in fields)
        conn.execute(
            f"UPDATE trades SET {set_clause} WHERE trade_id=?",
            (*fields.values(), trade_id),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_empty_position(db_path, trade_id):
    """只有從未有過成交紀錄的部位才能真的取消；已有歷史的部位回傳 False，
    呼叫端應該改請使用者用新增成交把股數全部賣出（讓 _recalculate_rollup
    自然把狀態轉成 CLOSED，保留真正的交易紀錄，而不是把歷史資料丟掉）。"""
    conn = _connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM trade_executions WHERE trade_id=?", (trade_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    if count > 0:
        return False
    cancel_trade(db_path, trade_id)
    return True


def migrate_legacy_position(db_path, position, initial_stop_loss):
    """把舊版 positions.json 的一筆 Position 搬進 trades：開一筆交易＋補一筆
    對應的 BUY 成交（價格/股數/日期都沿用舊資料），讓搬移後的部位資料
    （總股數、均價）跟搬移前完全一致。回傳新交易的 trade_id。"""
    trade_id = open_trade(
        db_path,
        symbol=position.ticker,
        side="LONG",
        initial_stop_loss=initial_stop_loss,
        initial_entry_price=position.entry_price,
        opened_at=position.entry_date,
        notes=position.note or None,
    )
    record_execution(
        db_path,
        trade_id,
        action="BUY",
        price=position.entry_price,
        qty=position.shares,
        executed_at=position.entry_date,
    )
    return trade_id
