#!/usr/bin/env python3
"""tradingnote - 終端機版本"""

import re
import unicodedata

from tradingnote_core import (
    compute_pnl,
    find_position,
    get_market_snapshot,
    load_settings,
    lookup_price,
    snapshot_staleness_warnings,
)
from tradingnote_history import (
    DEFAULT_BACKFILL_TARGET_DAYS,
    backfill_twse_history,
    compute_industry_flow,
    get_latest_ticker_record,
    record_snapshot,
)
from tradingnote_http import PriceFetchError
from tradingnote_legacy_positions import archive_legacy_positions_file, load_legacy_positions
from tradingnote_paths import APP_PATHS
from tradingnote_tradejournal import (
    cancel_empty_position,
    list_open_positions,
    migrate_legacy_position,
    open_position,
    record_execution,
    update_position_plan,
)

DATA_DIR = APP_PATHS.data_dir
POSITIONS_PATH = APP_PATHS.positions
CACHE_PATH = APP_PATHS.price_cache
HISTORY_DB_PATH = APP_PATHS.history_db
SETTINGS_PATH = APP_PATHS.settings

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def visible_width(s):
    plain = ANSI_RE.sub("", s)
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in plain)


def pad(s, width):
    return s + " " * max(0, width - visible_width(s))


def print_help():
    print(
        f"""
{BOLD}指令說明{RESET}
  add <代號> <股數> <成本價> <日期 YYYY-MM-DD> <停損價> [備註...]   開新部位
  buy <id> <股數> <價格> [成交時間]                                  加碼（新增一筆買進成交）
  sell <id> <股數> <價格> [成交時間]                                 減碼／出場（新增一筆賣出成交，股數歸零會自動收盤）
  edit <id> <停損價> [備註...]                                       編輯部位設定（只改停損價／備註，股數價格請用 buy/sell）
  list / ls                                                          列出目前所有（OPEN）部位，含現價與損益
  remove <id> / rm <id>                                              取消部位（僅限尚無成交記錄者）
  price <代號>                                                        查詢單一股票現價（不新增部位）
  refresh                                                            強制重新抓取台股價格（略過快取）
  flow                                                               列出產業資金流向（成交金額排序）
  backfill                                                           回補上市股票歷史資料（天數見 settings.json 的 backfill_target_days，僅需執行一次）
  help                                                                顯示這個說明
  quit / exit                                                        離開程式
"""
    )


def print_positions_table(positions, snapshot):
    if not positions:
        print("目前沒有任何部位，使用 add 指令新增。")
        return

    headers = [
        "id", "代號", "名稱", "股數", "成本價", "現價",
        "權益成本", "權益現值", "損益", "損益%", "備註",
    ]
    rows = []
    for pos in positions:
        price = lookup_price(pos.ticker, snapshot)
        pnl = compute_pnl(pos, price)
        if price is not None and price.name and price.name != pos.name:
            pos.name = price.name
            pos.market = price.market

        # 權益成本＝股數×成本價，不需要即時報價；權益現值＝股數×現價，price 抓不到時
        # 比照損益顯示 N/A（見 tradingnote_gui.py refresh_table 同樣的邏輯）。
        equity_cost = pos.shares * pos.entry_price

        if pnl is None:
            current, pnl_amt, pnl_pct = "N/A", "N/A", "N/A"
            equity_value = "N/A"
        else:
            current = f"{pnl.current_price:.2f}"
            color = GREEN if pnl.unrealized_pnl >= 0 else RED
            pnl_amt = f"{color}{pnl.unrealized_pnl:+.0f}{RESET}"
            pnl_pct = f"{color}{pnl.pnl_pct:+.2f}%{RESET}"
            equity_value = f"{pnl.current_value:,.0f}"

        rows.append(
            [
                pos.id,
                pos.ticker,
                pos.name or "-",
                str(pos.shares),
                f"{pos.entry_price:.2f}",
                current,
                f"{equity_cost:,.0f}",
                equity_value,
                pnl_amt,
                pnl_pct,
                pos.note or "",
            ]
        )

    widths = [
        max(visible_width(headers[i]), *(visible_width(r[i]) for r in rows))
        for i in range(len(headers))
    ]
    header_line = " │ ".join(pad(h, w) for h, w in zip(headers, widths))
    print(header_line)
    print("─┼─".join("─" * w for w in widths))
    for r in rows:
        print(" │ ".join(pad(c, w) for c, w in zip(r, widths)))


def print_price(ticker, snapshot):
    ticker = ticker.strip().upper()
    price = lookup_price(ticker, snapshot)
    if price is not None:
        print(
            f"{price.ticker} {price.name}（{price.market}）  "
            f"收盤 {price.close}  漲跌 {price.change}  開 {price.open} 高 {price.high} 低 {price.low}  "
            f"日期 {price.date}"
        )
        return

    # LIFO 備援：即時快照沒有這檔股票時，改向歷史資料庫要最新一筆（date DESC）。
    record = get_latest_ticker_record(HISTORY_DB_PATH, ticker)
    if record is None:
        print(f"查無此股票代號：{ticker}")
        return
    print(
        f"{ticker} {record['name']}（{record['market']}，取自歷史資料 {record['date']}）  "
        f"收盤 {record['close']}  成交量 {record['volume']}"
    )


def sync_history_continuity(target_days=DEFAULT_BACKFILL_TARGET_DAYS):
    """每次啟動都確保 TWSE 歷史資料連續（跟上到昨天）。只有真的需要補缺口時才會印
    進度；資料已經連續的情況下這個函式幾乎沒有輸出、也幾乎沒有耗時。target_days 讀自
    settings.json 的 backfill_target_days（跟 GUI「設定」頁的「回補天數」共用同一份）。"""
    fetched_any = []

    def on_progress(done, target):
        fetched_any.append(True)
        print(f"\r正在回補歷史資料缺口... {done}/{target} 天", end="", flush=True)

    done = backfill_twse_history(
        HISTORY_DB_PATH, target_days=target_days, on_progress=on_progress
    )
    if fetched_any:
        print(f"\n歷史資料缺口已補齊（共 {done} 個交易日）。")


def print_industry_flow(snapshot):
    flow = compute_industry_flow(HISTORY_DB_PATH, snapshot)
    if not flow:
        print("尚無產業資金流向資料，請先確認價格已抓取（且已執行過 backfill）。")
        return

    headers = ["產業", "檔數", "成交金額(億)", "加權漲跌%", "量比"]
    rows = []
    for f in flow:
        if f.avg_change_pct is None:
            change_str = "資料不足"
        else:
            color = GREEN if f.avg_change_pct >= 0 else RED
            change_str = f"{color}{f.avg_change_pct:+.2f}%{RESET}"
        ratio_str = f"{f.volume_ratio:.2f}" if f.volume_ratio is not None else "資料不足"
        rows.append(
            [
                f.industry,
                str(f.stock_count),
                f"{f.total_trading_value / 1e8:,.1f}",
                change_str,
                ratio_str,
            ]
        )

    widths = [
        max(visible_width(headers[i]), *(visible_width(r[i]) for r in rows))
        for i in range(len(headers))
    ]
    print(" │ ".join(pad(h, w) for h, w in zip(headers, widths)))
    print("─┼─".join("─" * w for w in widths))
    for r in rows:
        print(" │ ".join(pad(c, w) for c, w in zip(r, widths)))


def _resolve_trade_id(db_path, position_id):
    """沿用 find_position 的前綴比對，但只對目前 OPEN 的部位生效——這就是
    tradingnote_tradejournal._recalculate_rollup 只會把 OPEN 轉成 CLOSED、
    不會反向重新打開的直接後果：對非 OPEN 的部位操作沒有意義。"""
    position = find_position(list_open_positions(db_path), position_id)
    return int(position.id) if position is not None else None


def parse_command(cmd, db_path, snapshot_ref, cache_path, settings):
    parts = cmd.split()
    if not parts:
        return snapshot_ref

    head = parts[0].lower()

    if head in ("q", "quit", "exit"):
        raise SystemExit

    if head in ("h", "help"):
        print_help()
        return snapshot_ref

    if head in ("list", "ls"):
        print_positions_table(list_open_positions(db_path, snapshot_ref), snapshot_ref)
        return snapshot_ref

    if head == "refresh":
        try:
            snapshot_ref = get_market_snapshot(cache_path, force_refresh=True)
            record_snapshot(HISTORY_DB_PATH, snapshot_ref)
            print("已重新抓取台股價格。")
            for warning in snapshot_staleness_warnings(snapshot_ref):
                print(f"⚠ {warning}")
        except PriceFetchError as e:
            print(f"重新抓取失敗：{e}")
        return snapshot_ref

    if head == "price":
        if len(parts) < 2:
            print("用法：price <代號>")
            return snapshot_ref
        print_price(parts[1], snapshot_ref)
        return snapshot_ref

    if head == "flow":
        print_industry_flow(snapshot_ref)
        return snapshot_ref

    if head == "backfill":
        target_days = settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)
        print(f"開始回補上市股票近 {target_days} 個交易日的歷史資料...")

        def on_progress(done, target):
            print(f"\r已回補 {done}/{target} 天", end="", flush=True)

        done = backfill_twse_history(
            HISTORY_DB_PATH, target_days=target_days, on_progress=on_progress
        )
        print(f"\n回補完成，共 {done} 個交易日。")
        return snapshot_ref

    if head in ("remove", "rm"):
        if len(parts) < 2:
            print("用法：remove <id>")
            return snapshot_ref
        trade_id = _resolve_trade_id(db_path, parts[1])
        if trade_id is None:
            print("找不到這個 id。")
            return snapshot_ref
        if cancel_empty_position(db_path, trade_id):
            print("已取消。")
        else:
            print("這筆部位已經有成交記錄，請改用 sell 把股數全部賣出。")
        return snapshot_ref

    if head == "add":
        if len(parts) < 6:
            print("用法：add <代號> <股數> <成本價> <日期 YYYY-MM-DD> <停損價> [備註...]")
            return snapshot_ref
        ticker, shares_s, price_s, entry_date, stop_loss_s = parts[1:6]
        note = " ".join(parts[6:])
        try:
            shares = int(shares_s)
            entry_price = float(price_s)
            initial_stop_loss = float(stop_loss_s)
        except ValueError:
            print("股數需為整數、成本價與停損價需為數字。")
            return snapshot_ref
        try:
            pos = open_position(
                db_path, ticker=ticker, shares=shares, entry_price=entry_price,
                entry_date=entry_date, initial_stop_loss=initial_stop_loss,
                note=note, snapshot=snapshot_ref,
            )
        except ValueError as e:
            print(f"新增失敗：{e}")
            return snapshot_ref
        print(f"已新增部位 {pos.id}：{pos.ticker} {pos.name or ''}".rstrip())
        return snapshot_ref

    if head in ("buy", "sell"):
        if len(parts) < 4:
            print(f"用法：{head} <id> <股數> <價格> [成交時間]")
            return snapshot_ref
        position_id, shares_s, price_s = parts[1:4]
        executed_at = parts[4] if len(parts) > 4 else None
        trade_id = _resolve_trade_id(db_path, position_id)
        if trade_id is None:
            print("找不到這個 id（或這筆部位已經不是 OPEN 狀態）。")
            return snapshot_ref
        try:
            qty = float(shares_s)
            price = float(price_s)
        except ValueError:
            print("股數與價格需為數字。")
            return snapshot_ref
        record_execution(
            db_path, trade_id, action=head.upper(), price=price, qty=qty, executed_at=executed_at,
        )
        print("已記錄成交。")
        return snapshot_ref

    if head == "edit":
        if len(parts) < 3:
            print("用法：edit <id> <停損價> [備註...]")
            print("（股數／成本價／日期不可直接改，請用 buy/sell 調整部位）")
            return snapshot_ref
        position_id, stop_loss_s = parts[1:3]
        note = " ".join(parts[3:])
        trade_id = _resolve_trade_id(db_path, position_id)
        if trade_id is None:
            print("找不到這個 id（或這筆部位已經不是 OPEN 狀態）。")
            return snapshot_ref
        try:
            initial_stop_loss = float(stop_loss_s)
        except ValueError:
            print("停損價需為數字。")
            return snapshot_ref
        try:
            update_position_plan(
                db_path, trade_id, initial_stop_loss=initial_stop_loss, notes=note or None,
            )
        except ValueError as e:
            print(f"編輯失敗：{e}")
            return snapshot_ref
        print("已更新部位設定。")
        return snapshot_ref

    print("看不懂這個指令，輸入 help 查看說明。")
    return snapshot_ref


def _prompt_legacy_migration():
    """舊版 positions.json 一次性搬遷：trades.initial_stop_loss 是必填欄位，
    舊部位從來沒有停損價，逐筆詢問使用者補填。先收集完全部停損價才開始寫入，
    中途按 Ctrl-C／EOF 不會留下部分搬遷的狀態，原檔案也不會被改名，下次啟動
    會再問一次（跟 GUI 的 LegacyPositionMigrationDialog 走同一套保證）。"""
    legacy_positions = load_legacy_positions(POSITIONS_PATH)
    if not legacy_positions:
        return
    print(
        "偵測到舊版「部位紀錄」資料（positions.json）。新版把部位跟交易紀錄合併，"
        "每筆部位都需要一個初始停損價，請為以下每筆部位補填："
    )
    rows = []
    for position in legacy_positions:
        while True:
            raw = input(
                f"  {position.ticker}（{position.shares} 股 @ {position.entry_price}，"
                f"{position.entry_date}）停損價："
            ).strip()
            try:
                stop_loss = float(raw)
                if stop_loss <= 0:
                    raise ValueError
            except ValueError:
                print("    請輸入大於 0 的數字。")
                continue
            rows.append((position, stop_loss))
            break

    for position, stop_loss in rows:
        migrate_legacy_position(HISTORY_DB_PATH, position, stop_loss)
    archive_legacy_positions_file(POSITIONS_PATH)
    print("搬遷完成，positions.json 已改名為 positions.json.bak。\n")


def main():
    print(f"{BOLD}=== tradingnote 台股部位紀錄 ==={RESET}\n")
    try:
        _prompt_legacy_migration()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消搬遷，下次啟動會再次詢問。")
        return

    try:
        snapshot = get_market_snapshot(CACHE_PATH)
        record_snapshot(HISTORY_DB_PATH, snapshot)
        for warning in snapshot_staleness_warnings(snapshot):
            print(f"⚠ {warning}")
    except PriceFetchError as e:
        print(f"價格資料抓取失敗（{e}），將以無價格模式啟動。")
        snapshot = {}

    settings = load_settings(SETTINGS_PATH)
    if settings.get("auto_check_continuity", True):
        try:
            sync_history_continuity(
                settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)
            )
        except PriceFetchError:
            pass  # 資料連續性檢查失敗不應該擋住程式啟動，之後仍可用 backfill 指令手動重試

    print_positions_table(list_open_positions(HISTORY_DB_PATH, snapshot), snapshot)
    print_help()

    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再見！")
            break
        try:
            snapshot = parse_command(cmd, HISTORY_DB_PATH, snapshot, CACHE_PATH, settings)
        except SystemExit:
            print("再見！")
            break


if __name__ == "__main__":
    main()
