"""架構規則的自動檢查（AST 靜態分析，不 import 任何被檢查的模組）。

規則來源是 ARCHITECTURE.md；這裡失敗代表程式碼跟文件寫的依賴方向／表擁有權不一致，
修程式或（確定是刻意的設計時）同步改 ARCHITECTURE.md 與這裡的允許清單。"""

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_SKIP_DIRS = {".venv", "__pycache__", "build", "dist", "tools"}

# GUI 專用的根目錄模組（可以用 PySide6）；其餘 tradingnote_*.py 都算核心模組
GUI_ONLY = {"tradingnote_gui", "tradingnote_tasks", "tradingnote_single_instance"}
UI_PACKAGES = {"PySide6", "pyqtgraph", "ui", "tradingnote_gui", "tradingnote_tasks", "tradingnote_single_instance"}
# 刻意的第三方 lazy import：(模組, 套件)
ALLOWED_THIRD_PARTY = {("tradingnote_esun", "esun_marketdata")}
# ui 依賴方向：只能 import 同層或更低層（數字小＝越底層）
UI_RANK = {
    "tradingnote_gui": 6, "ui.tabs": 5, "ui.pages": 4, "ui.dialogs": 3, "ui.charts": 2,
    "ui.stock_charts": 2, "ui.badges": 1, "ui.widgets": 1, "ui.workers": 1,
    "ui.components": 0, "ui.format": 0, "ui.theme": 0, "ui.app_paths": 0,
}


def _module_name(path):
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _production_modules():
    mods = {}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if set(rel.parts) & _SKIP_DIRS or rel.parts[0].startswith("_qa") or path.name.startswith("test_"):
            continue
        mods[_module_name(path)] = path
    return mods


MODULES = _production_modules()
TREES = {m: ast.parse(p.read_text(encoding="utf-8"), str(p)) for m, p in MODULES.items()}
SOURCES = {m: p.read_text(encoding="utf-8") for m, p in MODULES.items()}
CORE = sorted(m for m in MODULES if m.startswith("tradingnote_") and m not in GUI_ONLY)
PROJECT_ROOTS = {m.split(".")[0] for m in MODULES}


def _imports(module):
    """[(被 import 的模組, 行號, 是否在函式／類別外)]；from pkg import sub 也展開成 pkg.sub。"""
    tree = TREES[module]
    nested = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested.update(id(n) for n in ast.walk(node) if n is not node)
    found = []
    for node in ast.walk(tree):
        top = id(node) not in nested
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno, top) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno, top))
            found += [(f"{node.module}.{a.name}", node.lineno, top)
                      for a in node.names if f"{node.module}.{a.name}" in MODULES]
    return found


def _table_owners():
    owners = {}
    for module, source in SOURCES.items():
        for table in re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", source, re.I):
            owners.setdefault(table, set()).add(module)
    return owners


class ArchitectureTests(unittest.TestCase):
    def assertNoViolations(self, violations, rule):
        self.assertEqual(violations, [], f"{rule}：\n  " + "\n  ".join(map(str, violations)))

    def test_core_modules_do_not_import_ui(self):
        violations = [f"{m}:{ln} → {t}" for m in CORE for t, ln, _ in _imports(m)
                      if t.split(".")[0] in UI_PACKAGES]
        self.assertNoViolations(violations, "核心模組不能 import UI")

    def test_core_modules_and_cli_use_only_stdlib(self):
        stdlib = set(sys.stdlib_module_names)
        violations = []
        for m in CORE + ["tradingnote"]:
            for t, ln, _ in _imports(m):
                root = t.split(".")[0]
                if root not in stdlib and root not in PROJECT_ROOTS and (m, root) not in ALLOWED_THIRD_PARTY:
                    violations.append(f"{m}:{ln} → {t}")
        self.assertNoViolations(violations, "核心模組／CLI 只能用標準函式庫")

    def test_core_modules_have_no_import_cycles(self):
        graph = {m: {t.split(".")[0] for t, _, _ in _imports(m)} & set(CORE) - {m} for m in CORE}
        cycles = []

        def visit(node, stack):
            for nxt in sorted(graph[node]):
                if nxt in stack:
                    cycles.append(" → ".join(stack[stack.index(nxt):] + [nxt]))
                elif len(stack) < len(CORE):
                    visit(nxt, stack + [nxt])

        for module in CORE:
            visit(module, [module])
        self.assertNoViolations(sorted(set(cycles)), "核心模組之間不能循環 import（含函式內 lazy import）")

    def test_ui_dependency_direction(self):
        def rank(name):
            for prefix in sorted(UI_RANK, key=len, reverse=True):
                if name == prefix or name.startswith(prefix + "."):
                    return UI_RANK[prefix]
            return None

        violations = []
        for m in MODULES:
            if not (m.startswith("ui") or m == "tradingnote_gui"):
                continue
            for t, ln, _ in _imports(m):
                if m.startswith("ui") and t == "tradingnote_gui":
                    violations.append(f"{m}:{ln} → tradingnote_gui")
                src, dst = rank(m), rank(t)
                if src is not None and dst is not None and dst > src:
                    violations.append(f"{m}:{ln} → {t}")
        self.assertNoViolations(violations, "ui 只能往下層 import（tabs → dialogs → charts → widgets…）")

    def test_app_paths_read_as_attributes(self):
        violations = [f"{m}:{n.lineno}" for m, tree in TREES.items() for n in ast.walk(tree)
                      if isinstance(n, ast.ImportFrom) and n.module == "ui.app_paths"]
        self.assertNoViolations(violations, "不要 from ui.app_paths import 名稱（測試覆寫會失效）")

    def test_background_task_handles_are_kept(self):
        violations = []
        for m, tree in TREES.items():
            for n in ast.walk(tree):
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                    f = n.value.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if name in ("run_background_task", "run_task_in_thread"):
                        violations.append(f"{m}:{n.lineno}")
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Thread"
                        and (m.startswith("ui") or m == "tradingnote_gui")):
                    violations.append(f"{m}:{n.lineno} threading.Thread（改用 run_background_task）")
        self.assertNoViolations(violations, "背景任務回傳值要留住、GUI 不自己開執行緒")

    def test_no_foreign_connect(self):
        violations = []
        for m, tree in TREES.items():
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and n.attr == "_connect" and isinstance(n.value, ast.Name):
                    violations.append(f"{m}:{n.lineno} {n.value.id}._connect")
                if isinstance(n, ast.ImportFrom) and any(a.name == "_connect" for a in n.names):
                    violations.append(f"{m}:{n.lineno} from {n.module} import _connect")
        self.assertNoViolations(violations, "不要呼叫別的模組的 _connect()，改用擁有模組的公開函式")

    def test_tables_have_one_documented_owner_and_no_foreign_writes(self):
        owners = _table_owners()
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        violations = [f"{t} 有多個模組建立：{sorted(ms)}" for t, ms in owners.items() if len(ms) > 1]
        violations += [f"{t}（{', '.join(sorted(ms))}）沒寫進 ARCHITECTURE.md「資料儲存」"
                       for t, ms in owners.items() if f"`{t}`" not in architecture]
        write_re = re.compile(r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)
        for m, source in SOURCES.items():
            for lineno, line in enumerate(source.splitlines(), 1):
                for table in write_re.findall(line):
                    if table in owners and m not in owners[table]:
                        violations.append(f"{m}:{lineno} 寫入 {table}（擁有者 {sorted(owners[table])}）")
        self.assertNoViolations(violations, "每張表只有一個擁有模組、寫進文件、只有擁有者能寫")

    def test_http_and_endpoints_are_centralised(self):
        violations = []
        for m, tree in TREES.items():
            if m in ("tradingnote_http", "tradingnote_api_config"):
                continue
            for t, ln, _ in _imports(m):
                if t in ("urllib.request", "http.client", "http.cookiejar"):
                    violations.append(f"{m}:{ln} import {t}（改用 tradingnote_http）")
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.match(r"https?://", n.value):
                    violations.append(f"{m}:{n.lineno} {n.value[:70]}（放 tradingnote_api_config）")
        self.assertNoViolations(violations, "對外 HTTP 只走 tradingnote_http、端點網址集中在 tradingnote_api_config")


if __name__ == "__main__":
    unittest.main()
