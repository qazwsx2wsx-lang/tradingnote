#!/usr/bin/env python3
"""tradingnote - 終端機版本"""

import re
import unicodedata
from pathlib import Path

from tradingnote_core import (
    PriceFetchError,
    add_position,
    compute_pnl,
    get_market_snapshot,
    load_positions,
    load_settings,
    lookup_price,
    remove_position,
    save_positions,
    snapshot_staleness_warnings,
    update_position,
)
from tradingnote_history import (
    DEFAULT_BACKFILL_TARGET_DAYS,
    backfill_twse_history,
    compute_industry_flow,
    get_latest_ticker_record,
    record_snapshot,
)

DATA_DIR = Path(__file__).parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
CACHE_PATH = DATA_DIR / "price_cache.json"
HISTORY_DB_PATH = DATA_DIR / "history.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

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
  add <代號> <股數> <成本價> <日期 YYYY-MM-DD> [備註...]   新增部位
  edit <id> <股數> <成本價> <日期 YYYY-MM-DD> [備註...]    編輯部位（id 可用前綴，代號不可改）
  list / ls                                                列出所有部位（含現價與損益）
  remove <id> / rm <id>                                    刪除部位（id 可用前綴）
  price <代號>                                              查詢單一股票現價（不新增部位）
  refresh                                                  強制重新抓取台股價格（略過快取）
  flow                                                     列出產業資金流向（成交金額排序）
  backfill                                                 回補上市股票歷史資料（天數見 settings.json 的 backfill_target_days，僅需執行一次）
  help                                                      顯示這個說明
  quit / exit                                              離開程式
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


def parse_command(cmd, positions, snapshot_ref, cache_path, settings):
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
        print_positions_table(positions, snapshot_ref)
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
        if remove_position(positions, parts[1]):
            print("已刪除。")
        else:
            print("找不到這個 id。")
        return snapshot_ref

    if head == "add":
        if len(parts) < 5:
            print("用法：add <代號> <股數> <成本價> <日期 YYYY-MM-DD> [備註...]")
            return snapshot_ref
        ticker, shares_s, price_s, entry_date = parts[1:5]
        note = " ".join(parts[5:])
        try:
            shares = int(shares_s)
            entry_price = float(price_s)
        except ValueError:
            print("股數需為整數、成本價需為數字。")
            return snapshot_ref
        try:
            pos = add_position(
                positions, ticker, shares, entry_price, entry_date, note, snapshot_ref
            )
        except ValueError as e:
            print(f"新增失敗：{e}")
            return snapshot_ref
        print(f"已新增部位 {pos.id}：{pos.ticker} {pos.name or ''}".rstrip())
        return snapshot_ref

    if head == "edit":
        if len(parts) < 5:
            print("用法：edit <id> <股數> <成本價> <日期 YYYY-MM-DD> [備註...]")
            print("（代號不可修改，換股票請 remove 後重新 add）")
            return snapshot_ref
        position_id, shares_s, price_s, entry_date = parts[1:5]
        note = " ".join(parts[5:])
        try:
            shares = int(shares_s)
            entry_price = float(price_s)
        except ValueError:
            print("股數需為整數、成本價需為數字。")
            return snapshot_ref
        try:
            pos = update_position(
                positions, position_id, shares, entry_price, entry_date, note, snapshot_ref
            )
        except ValueError as e:
            print(f"編輯失敗：{e}")
            return snapshot_ref
        print(f"已更新部位 {pos.id}：{pos.ticker} {pos.name or ''}".rstrip())
        return snapshot_ref

    print("看不懂這個指令，輸入 help 查看說明。")
    return snapshot_ref


def main():
    print(f"{BOLD}=== tradingnote 台股部位紀錄 ==={RESET}\n")
    positions = load_positions(POSITIONS_PATH)
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

    print_positions_table(positions, snapshot)
    print_help()

    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再見！")
            break
        try:
            snapshot = parse_command(cmd, positions, snapshot, CACHE_PATH, settings)
        except SystemExit:
            print("再見！")
            break
        finally:
            save_positions(POSITIONS_PATH, positions)


if __name__ == "__main__":
    main()
