"""GUI 背景工作：回補、重新整理、啟動預載等包在 run_background_task 上的流程。"""

from tradingnote_core import fetch_tpex_valuation_all, fetch_twse_valuation_all, get_market_snapshot
from tradingnote_finmind import backfill_tpex_history_via_finmind
from tradingnote_history import (
    backfill_twse_history,
    get_industry_map,
    record_snapshot,
    record_valuation_snapshot,
)
from tradingnote_http import PriceFetchError
from tradingnote_institutional import get_cached_institutional_snapshot
from tradingnote_tasks import run_background_task

from ui import app_paths


def _record_valuation_snapshot_best_effort():
    """盡力而為記錄今天的本益比／股價淨值比快照（TWSE／TPEX 官方 bulk 端點各打
    一次），供泡泡圖「估值百分位」新模式逐日累積用（見 tradingnote_history.
    compute_valuation_flow）。刻意用寬鬆的 `except Exception`（不是專案慣例的窄範圍
    例外）：這是輔助性的背景累積動作，任何失敗（離線、端點暫時掛掉、格式意外跑掉）
    都不應該讓啟動或「重新整理」流程跟著失敗——當天只是沒新增一筆 valuation_history，
    不影響其他既有功能，之後正常連線時自然會補上。"""
    try:
        record_valuation_snapshot(app_paths.HISTORY_DB_PATH, fetch_twse_valuation_all(), "TWSE")
    except Exception:
        pass
    try:
        record_valuation_snapshot(app_paths.HISTORY_DB_PATH, fetch_tpex_valuation_all(), "TPEX")
    except Exception:
        pass


def run_backfill_in_thread(parent, target_days, progress_cb, done_cb, error_cb):
    def work(_cancel_event, emit):
        return backfill_twse_history(
            app_paths.HISTORY_DB_PATH,
            target_days=target_days,
            on_progress=lambda done, total: emit(done, total),
        )

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )


def run_tpex_finmind_backfill_in_thread(
    parent, token, target_days, progress_cb, done_cb, error_cb
):
    def work(_cancel_event, emit):
        return backfill_tpex_history_via_finmind(
            app_paths.HISTORY_DB_PATH,
            token,
            target_days=target_days,
            on_progress=lambda done, total, ticker: emit(done, total, ticker),
        )

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )


def run_task_in_thread(parent, work_fn, on_done, on_error):
    return run_background_task(
        parent,
        lambda _cancel_event, _emit: work_fn(),
        on_done,
        on_error,
    )


def run_refresh_in_thread(parent, progress_cb, done_cb, error_cb):
    total_steps = 5

    def work(_cancel_event, emit):
        snapshot = get_market_snapshot(
            app_paths.CACHE_PATH,
            force_refresh=True,
            on_progress=lambda done, _total, label: emit(done, total_steps, label),
        )
        emit(3, total_steps, "正在取得三大法人方向...")
        try:
            get_cached_institutional_snapshot(
                app_paths.INSTITUTIONAL_CACHE_PATH, force_refresh=True
            )
        except PriceFetchError:
            pass
        emit(4, total_steps, "正在寫入歷史資料庫...")
        record_snapshot(app_paths.HISTORY_DB_PATH, snapshot)
        _record_valuation_snapshot_best_effort()
        emit(5, total_steps, "重新整理完成。")
        return snapshot

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )


def run_startup_preload_in_thread(parent, progress_cb, done_cb, error_cb):
    total_steps = 5

    def work(_cancel_event, emit):
        snapshot = get_market_snapshot(
            app_paths.CACHE_PATH,
            on_progress=lambda done, _total, label: emit(done, total_steps, label),
        )
        emit(3, total_steps, "正在取得三大法人方向...")
        try:
            get_cached_institutional_snapshot(app_paths.INSTITUTIONAL_CACHE_PATH)
        except PriceFetchError:
            pass
        emit(4, total_steps, "正在寫入歷史資料庫...")
        record_snapshot(app_paths.HISTORY_DB_PATH, snapshot)
        _record_valuation_snapshot_best_effort()
        emit(4, total_steps, "正在更新產業分類...")
        get_industry_map(app_paths.HISTORY_DB_PATH)
        emit(5, total_steps, "啟動準備完成。")
        return snapshot

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )
