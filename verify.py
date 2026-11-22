"""收工前的自我驗證：全綠才算完成（規則見 AGENTS.md「收工前自我驗證」）。

用法（一律用專案 .venv）：
    .venv/Scripts/python.exe verify.py          # 手動跑，exit 0＝通過
    .venv/Scripts/python.exe verify.py --hook   # Claude Code Stop hook 用（見 .claude/settings.json）

檢查項目：
  1. 是不是用專案 .venv 的 Python 執行（系統 Python 3.14 會 SSL 驗證失敗）
  2. 所有專案 .py 能編譯（語法錯誤最快抓出來）
  3. 全部 unittest（含 test_architecture.py 架構規則、offscreen GUI 測試）

輸出刻意精簡：通過只印一行；失敗只印失敗測試的 traceback，避免浪費 token。
測試一律指到臨時資料目錄（TRADINGNOTE_DATA_DIR），不會碰到 data/ 的正式資料庫。

--hook 模式：.py 內容跟上次通過時一樣就直接放行（只改文件不重跑）；
失敗時 exit 2 讓 Claude 繼續修，連續失敗 MAX_HOOK_BLOCKS 次後放行並要求在 STATUS.md 註明，避免無限迴圈。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {".venv", "__pycache__", "build", "dist", "data", ".git", ".claude"}
STAMP = ROOT / ".claude" / "verify_stamp.json"
MAX_HOOK_BLOCKS = 3
MAX_OUTPUT_LINES = 120


def _project_py_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0].startswith("_qa") or any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield path


def _source_hash():
    digest = hashlib.sha256()
    for path in _project_py_files():
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _check_interpreter():
    venv = (ROOT / ".venv").resolve()
    if Path(sys.prefix).resolve() != venv:
        return [f"沒有用專案 .venv 執行（目前 {sys.executable}）；請改用 .venv/Scripts/python.exe verify.py"]
    return []


def _check_compile():
    errors = []
    for path in _project_py_files():
        try:
            compile(path.read_bytes(), str(path), "exec")
        except (SyntaxError, ValueError) as exc:
            errors.append(f"語法錯誤：{path.relative_to(ROOT)}\n{exc}")
    return errors


def _failure_blocks(output):
    """只留 unittest 的 FAIL/ERROR 區塊與最後的統計行。"""
    lines = output.splitlines()
    keep, inside = [], False
    for line in lines:
        if line.startswith(("FAIL:", "ERROR:")):
            inside = True
        elif line.startswith("Ran ") and " test" in line:
            inside = False
        if inside:
            keep.append(line)
    tail = [line for line in lines[-5:] if line.strip()]
    keep = keep or lines[-40:]
    if len(keep) > MAX_OUTPUT_LINES:
        keep = keep[:MAX_OUTPUT_LINES] + [f"…（還有 {len(keep) - MAX_OUTPUT_LINES} 行省略）"]
    return "\n".join(keep + [""] + tail)


def _run_tests():
    with tempfile.TemporaryDirectory(prefix="tradingnote_verify_") as data_dir:
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
                   TRADINGNOTE_DATA_DIR=data_dir)
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-p", "test_*.py"],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    output = proc.stdout + proc.stderr
    summary = next((line for line in reversed(output.splitlines()) if line.startswith("Ran ")), "")
    if proc.returncode != 0:
        return [f"單元測試失敗（{summary}）\n{_failure_blocks(output)}"], summary
    return [], summary


def run_all():
    errors = _check_interpreter()
    if errors:
        return errors, ""
    errors = _check_compile()
    if errors:
        return errors, ""
    return _run_tests()


def _load_stamp():
    try:
        return json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_stamp(data):
    try:
        STAMP.parent.mkdir(exist_ok=True)
        STAMP.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def main_hook():
    try:
        json.load(sys.stdin)  # Stop hook 的 payload；目前不需要內容，讀掉即可
    except ValueError:
        pass
    stamp = _load_stamp()
    source_hash = _source_hash()
    if stamp.get("passed_hash") == source_hash:
        return 0
    errors, summary = run_all()
    if not errors:
        _save_stamp({"passed_hash": source_hash, "blocks": 0})
        print(f"verify.py 通過：{summary}")
        return 0
    blocks = stamp.get("blocks", 0) + 1
    if blocks > MAX_HOOK_BLOCKS:
        _save_stamp({"passed_hash": stamp.get("passed_hash"), "blocks": 0})
        print(f"verify.py 已連續失敗 {MAX_HOOK_BLOCKS} 次，先放行；"
              "必須在 STATUS.md 必讀簡介寫明哪個檢查沒過、原因與下一步。", file=sys.stderr)
        return 0
    _save_stamp({"passed_hash": stamp.get("passed_hash"), "blocks": blocks})
    print(f"收工前自我驗證沒過（第 {blocks}/{MAX_HOOK_BLOCKS} 次），修好再收工：\n\n"
          + "\n\n".join(errors), file=sys.stderr)
    return 2


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if "--hook" in sys.argv[1:]:
        return main_hook()
    errors, summary = run_all()
    if errors:
        print("驗證失敗：\n\n" + "\n\n".join(errors))
        return 1
    print(f"驗證通過：{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
