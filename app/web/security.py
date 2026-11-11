"""Проверки входящих вебхуков (без внешних зависимостей — покрыто тестами)."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json


# IP-адреса, с которых ЮKassa отправляет уведомления
# https://yookassa.ru/developers/using-api/webhooks
YOOKASSA_NETWORKS = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "77.75.154.128/25",
    "2a02:5180::/32",
)

_NETS = [ipaddress.ip_network(n) for n in YOOKASSA_NETWORKS]


def safe_eq(a: str | bytes | None, b: str | bytes | None) -> bool:
    """Сравнение секретов за постоянное время, устойчивое к «неудобным» символам.

    ``hmac.compare_digest`` для строк с не-ASCII символами бросает TypeError — сервер отвечал 500 вместо 401/404.
    Сравниваем байты (UTF-8), а любые странные входные данные просто дают False.
    """
    if a is None or b is None:
        return False
    try:
        ab = a if isinstance(a, bytes) else a.encode("utf-8", "surrogatepass")
        bb = b if isinstance(b, bytes) else b.encode("utf-8", "surrogatepass")
    except (UnicodeError, AttributeError):
        return False
    return hmac.compare_digest(ab, bb)


def ip_allowed(ip: str) -> bool:
    """IP входит в список адресов, с которых ЮKassa шлёт уведомления."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(addr in net for net in _NETS)


def verify_signature(raw: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 тела запроса, как подписывает Remnawave (заголовок X-Remnawave-Signature).

    Панель подписывает JSON.stringify(body) — компактный JSON. Пробуем и сырое тело, и перекодированное компактно.
    """
    if not secret or not signature:
        return False
    candidates = [raw]
    try:
        candidates.append(json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False).encode())
    except ValueError:
        pass
    sig = signature.strip().lower()
    return any(safe_eq(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), sig) for body in candidates)


def verify_cryptobot_signature(raw: bytes, signature: str, token: str) -> bool:
    """crypto-pay-api-signature = HMAC-SHA256(тело запроса, ключ = SHA256(токен приложения)), hex."""
    if not raw or not signature or not token:
        return False
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return safe_eq(expected, signature.strip().lower())
