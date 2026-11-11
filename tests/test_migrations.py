"""Схема в миграциях Alembic должна совпадать с моделями (имена таблиц и колонок, индексы)."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def model_schema() -> tuple[dict[str, set[str]], set[str]]:
    tree = ast.parse((ROOT / "app/db/models.py").read_text())
    tables: dict[str, set[str]] = {}
    indexes: set[str] = set()
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        name, cols = None, set()
        for st in cls.body:
            if isinstance(st, ast.Assign) and getattr(st.targets[0], "id", "") == "__tablename__":
                name = st.value.value
            if isinstance(st, ast.Assign) and getattr(st.targets[0], "id", "") == "__table_args__":
                for el in st.value.elts:
                    if isinstance(el, ast.Call) and ast.unparse(el.func) == "Index":
                        indexes.add(el.args[0].value)
            if isinstance(st, ast.AnnAssign) and isinstance(st.value, ast.Call) and ast.unparse(st.value.func) in ("mapped_column", "_dt"):
                cols.add(st.target.id)
                for kw in st.value.keywords:
                    if kw.arg == "index" and getattr(kw.value, "value", False) is True:
                        indexes.add(f"ix_{name}_{st.target.id}")
        if name:
            tables[name] = cols
    return tables, indexes


def migration_schema() -> tuple[dict[str, set[str]], set[str]]:
    tables: dict[str, set[str]] = {}
    indexes: set[str] = set()
    for f in sorted((ROOT / "alembic/versions").glob("*.py")):
        tree = ast.parse(f.read_text())
        # учитываем только upgrade(): downgrade() намеренно «ломает» схему и в сравнении не участвует
        ups = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"]
        for node in (x for up in ups for x in ast.walk(up)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            fn = node.func.attr
            if fn == "create_table":
                tname = node.args[0].value
                tables[tname] = {a.args[0].value for a in node.args[1:] if isinstance(a, ast.Call) and ast.unparse(a.func) == "sa.Column"}
            elif fn == "add_column":
                tables[node.args[0].value].add(node.args[1].args[0].value)
            elif fn == "drop_column":
                tables[node.args[0].value].discard(node.args[1].value)
            elif fn == "create_index":
                first = node.args[0]
                if isinstance(first, ast.Call):  # op.f("ix_…")
                    indexes.add(first.args[0].value)
                else:
                    indexes.add(first.value)
    return tables, indexes


class MigrationMatchesModels(unittest.TestCase):
    def test_tables_and_columns(self):
        m, _ = model_schema()
        g, _ = migration_schema()
        self.assertEqual(set(m), set(g), "наборы таблиц различаются")
        for t in m:
            self.assertEqual(m[t], g[t], f"таблица {t}: колонки в моделях и миграциях различаются")

    def test_indexes(self):
        _, mi = model_schema()
        _, gi = migration_schema()
        self.assertEqual(mi, gi, f"индексы различаются: {mi ^ gi}")

    def test_single_head(self):
        revs = {}
        for f in (ROOT / "alembic/versions").glob("*.py"):
            src = f.read_text()
            rev = re.search(r'^revision = "(\w+)"', src, re.M).group(1)
            down = re.search(r"^down_revision = (.+)$", src, re.M).group(1).strip().strip('"')
            revs[rev] = None if down == "None" else down
        heads = set(revs) - {d for d in revs.values() if d}
        self.assertEqual(len(heads), 1, f"должна быть одна «голова» миграций, а их {heads}")


if __name__ == "__main__":
    unittest.main()
