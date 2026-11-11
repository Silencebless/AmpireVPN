#!/usr/bin/env python3
"""Статическая проверка проекта без запуска (не требует установленных библиотек).

Проверяет:
  1. синтаксис всех .py;
  2. неопределённые имена (опечатки, забытые импорты);
  3. импорты внутри пакета app (модуль и имя существуют);
  4. каждая callback-кнопка имеет обработчик;
  5. каждое ask(..., "action") имеет @on_input("action");
  6. ключи и переменные в texts.render(...) совпадают с реестром app/texts.py;
  7. ключи ctx.cfg.get/set(...) существуют в DEFAULTS.

Запуск:  python scripts/check_code.py
"""
from __future__ import annotations

import ast
import builtins
import re
import symtable
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
errors: list[str] = []


def err(path: Path, line: int | None, msg: str) -> None:
    errors.append(f"{path.relative_to(ROOT)}:{line or 0}: {msg}")


files = sorted(p for p in APP.rglob("*.py"))
trees: dict[Path, ast.AST] = {}

# ───────────── 1. синтаксис ─────────────
for p in files:
    try:
        trees[p] = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    except SyntaxError as e:
        err(p, e.lineno, f"SyntaxError: {e.msg}")

# ───────────── 2. неопределённые имена ─────────────
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__"}


def module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def walk_tables(table: symtable.SymbolTable, defined: set[str], path: Path) -> None:
    for sym in table.get_symbols():
        if sym.is_global() and sym.is_referenced() and not sym.is_assigned():
            n = sym.get_name()
            if n not in defined and n not in BUILTINS:
                err(path, table.get_lineno(), f"неопределённое имя '{n}' (в {table.get_name()})")
    for child in table.get_children():
        walk_tables(child, defined, path)


for p, tree in trees.items():
    src = p.read_text(encoding="utf-8")
    top = symtable.symtable(src, str(p), "exec")
    module_level = {
        s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()
    }
    # имена, объявленные через global внутри функций
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            module_level.update(node.names)
    walk_tables(top, module_level, p)
    for s in top.get_symbols():
        if s.is_referenced() and not (s.is_assigned() or s.is_imported() or s.is_namespace()):
            if s.get_name() not in BUILTINS:
                err(p, 0, f"неопределённое имя на уровне модуля '{s.get_name()}'")

# ───────────── 3. внутренние импорты ─────────────
def module_path(mod: str) -> Path | None:
    base = ROOT.joinpath(*mod.split("."))
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    return None


def top_level_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in tree.body:  # type: ignore[attr-defined]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, (ast.If, ast.Try)):
            sub = ast.Module(body=node.body + getattr(node, "orelse", []), type_ignores=[])
            out |= top_level_names(sub)
    return out


for p, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            target = module_path(node.module)
            if target is None:
                err(p, node.lineno, f"нет модуля {node.module}")
                continue
            names = top_level_names(trees.get(target) or ast.parse(target.read_text(encoding="utf-8")))
            for a in node.names:
                if a.name == "*":
                    continue
                if a.name not in names and module_path(f"{node.module}.{a.name}") is None:
                    err(p, node.lineno, f"в {node.module} нет имени '{a.name}'")

# ───────────── 4. callback-кнопки ↔ обработчики ─────────────
exact: set[str] = set()
prefixes: set[str] = set()
for p, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "callback_query":
            for arg in node.args:
                text = ast.unparse(arg)
                m = re.fullmatch(r"F\.data == '(.+)'", text)
                if m:
                    exact.add(m.group(1))
                m = re.fullmatch(r"F\.data\.startswith\('(.+)'\)", text)
                if m:
                    prefixes.add(m.group(1))

CB_START = re.compile(r"^(a:|menu:|pay:|setup:|dev:|dv:|ref:|promo:|legal:|autopay:|connect|noop$|sup:)")


def literal_prefix(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            if isinstance(v, ast.Constant):
                out += str(v.value)
            else:
                return out
        return out
    return None


def has_handler(lit: str, is_full: bool) -> bool:
    if is_full and lit in exact:
        return True
    return any(lit.startswith(pr) for pr in prefixes) or (not is_full and any(e.startswith(lit) for e in exact))


for p, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = ast.unparse(node.func)
            cands: list[ast.AST] = []
            if fname == "btn" and len(node.args) >= 2:
                cands.append(node.args[1])
            elif fname in ("confirm_kb",):
                cands += node.args[:2]
            elif fname == "ask" and len(node.args) >= 5:
                cands.append(node.args[4])
            elif fname in ("back_kb",) and node.args:
                cands.append(node.args[0])
            for c in cands:
                lit = literal_prefix(c)
                if lit and CB_START.match(lit):
                    full = isinstance(c, ast.Constant)
                    if not has_handler(lit, full):
                        err(p, node.lineno, f"кнопка '{lit}' без обработчика")

# ───────────── 5. ask(...) ↔ on_input ─────────────
inputs: set[str] = set()
asked: list[tuple[Path, int, str]] = []
for p, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = ast.unparse(node.func)
            if fname == "on_input" and node.args and isinstance(node.args[0], ast.Constant):
                inputs.add(node.args[0].value)
            if fname == "ask" and len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                asked.append((p, node.lineno, node.args[2].value))
for p, line, action in asked:
    if action not in inputs:
        err(p, line, f"ask('{action}') без @on_input")

# ───────────── 6. тексты ─────────────
sys.path.insert(0, str(ROOT))
from app.texts import TEXTS  # noqa: E402

for p, tree in trees.items():
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "render"
            and node.args
        ):
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                key = a0.value
                if key not in TEXTS:
                    err(p, node.lineno, f"неизвестный текст '{key}'")
                    continue
                if any(k.arg is None for k in node.keywords):
                    continue
                given = {k.arg for k in node.keywords}
                want = set(TEXTS[key].vars)
                if given != want:
                    err(p, node.lineno, f"render('{key}'): переданы {sorted(given)}, нужны {sorted(want)}")
            elif isinstance(a0, ast.JoinedStr):
                pre = literal_prefix(a0) or ""
                if not any(k.startswith(pre) for k in TEXTS):
                    err(p, node.lineno, f"render(f'{pre}…'): нет текстов с таким префиксом")

# ───────────── 7. настройки ─────────────
rs_tree = ast.parse((APP / "services" / "runtime_settings.py").read_text(encoding="utf-8"))
default_keys: set[str] = set()
for node in ast.walk(rs_tree):
    if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "DEFAULTS" and isinstance(node.value, ast.Dict):
        default_keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
for p, tree in trees.items():
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "set", "reset", "is_overridden", "format_value")
            and ast.unparse(node.func.value).endswith("cfg")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            if node.args[0].value not in default_keys:
                err(p, node.lineno, f"cfg.{node.func.attr}('{node.args[0].value}'): нет такой настройки в DEFAULTS")

# ───────────── итог ─────────────
if errors:
    print(f"Найдено проблем: {len(errors)}\n")
    print("\n".join(errors))
    sys.exit(1)
print(f"OK: проверено {len(files)} файлов, {len(exact)} точных и {len(prefixes)} префиксных callback-обработчиков, "
      f"{len(inputs)} обработчиков ввода, {len(TEXTS)} текстов, {len(default_keys)} настроек.")
