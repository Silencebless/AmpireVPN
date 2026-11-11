"""Регрессия для аудита (п. 2): внутри `async with ctx.session()` нельзя ходить по сети.

Пока открыта сессия, занято соединение пула и (при FOR UPDATE) блокирована строка. Если панель Remnawave,
ЮKassa или Telegram тормозят, такие вызовы выедают пул — и встаёт весь бот. Тест разбирает исходники (AST)
и падает, если кто-то снова добавит сетевой вызов внутрь блока сессии.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "app/services/subscriptions.py",
    "app/services/payments.py",
    "app/services/referrals.py",
    "app/services/devices.py",
    "app/services/broadcast.py",
    "app/services/promos.py",
    "app/services/users.py",
    "app/services/backup.py",
]
FORBIDDEN_ATTRS = {"remna", "yk", "crypto", "bot", "notify"}          # клиенты внешних сервисов
FORBIDDEN_CALLS = {"_push", "_sync_locked", "sync_now", "sync_inside_lock", "resync_many", "list_devices", "trim_to_limit"}


def _chain(node: ast.AST) -> list[str]:
    out: list[str] = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        out.append(node.id)
    return out


def _is_session_block(node: ast.AsyncWith) -> bool:
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call) and _chain(call.func)[:1] in (["session"],) and "ctx" in _chain(call.func):
            return True
    return False


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    bad: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncWith) and _is_session_block(node)):
            continue
        for sub in (n for stmt in node.body for n in ast.walk(stmt)):
            if not isinstance(sub, ast.Await) or not isinstance(sub.value, ast.Call):
                continue
            chain = _chain(sub.value.func)
            if FORBIDDEN_ATTRS & set(chain[1:]) or chain[:1] and chain[0] in FORBIDDEN_CALLS:
                bad.append(f"{path.name}:{sub.lineno} await …{'.'.join(reversed(chain))}() внутри ctx.session()")
    return bad


class NoNetworkInTransactions(unittest.TestCase):
    def test_sources(self):
        problems: list[str] = []
        for rel in FILES:
            p = ROOT / rel
            if p.exists():
                problems += violations(p)
        self.assertEqual(problems, [], "сетевой вызов внутри транзакции БД:\n" + "\n".join(problems))

    def test_lint_actually_detects_the_old_pattern(self):
        src = (
            "async def f(self):\n"
            "    async with self.ctx.session() as s:\n"
            "        user = await s.get(User, 1)\n"
            "        info = await self._push(user)\n"
            "        await self.ctx.remna.update_user(1)\n"
        )
        tmp = ROOT / "tests" / "_lint_sample.py"
        tmp.write_text(src)
        try:
            found = violations(tmp)
        finally:
            tmp.unlink()
        self.assertEqual(len(found), 2)


if __name__ == "__main__":
    unittest.main()
