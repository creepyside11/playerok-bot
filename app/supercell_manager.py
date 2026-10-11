from __future__ import annotations

import asyncio
import email as email_pkg
import html
import imaplib
import logging
import re
import socket
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

logger = logging.getLogger("supercell_manager")

SUPERCELL_GAMES = {
    "brawl_stars": "Brawl Stars 🥊",
    "clash_royale": "Clash Royale 👑",
    "clash_of_clans": "Clash of Clans 🏰",
    "squad_busters": "Squad Busters 💥",
    "hay_day": "Hay Day 🚜",
}

# Известные IMAP сервера и провайдеры для авторегов
KNOWN_IMAP_DOMAINS: dict[str, str] = {
    # Firstmail официальные и зеркальные домены
    "firstmail.ltd": "imap.firstmail.ltd",
    "fmail.site": "imap.firstmail.ltd",
    "fmail.group": "imap.firstmail.ltd",
    "firstmail.com": "imap.firstmail.ltd",
    "firstmail.ru": "imap.firstmail.ltd",
    "eblanmail.com": "imap.firstmail.ltd",
    "eblanmail.ru": "imap.firstmail.ltd",
    "eblanmail.org": "imap.firstmail.ltd",
    # Notletters / Notmail / Mailtemp
    "notletters.com": "imap.notletters.com",
    "notmail.com": "imap.notletters.com",
    "notmail.ru": "imap.notletters.com",
    "notletters.ru": "imap.notletters.com",
    "notletter.com": "imap.notletters.com",
    # Стандартные сервисы
    "mail.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "rambler.ru": "imap.rambler.ru",
    "lenta.ru": "imap.rambler.ru",
    "autorambler.ru": "imap.rambler.ru",
    "ro.ru": "imap.rambler.ru",
    "yandex.ru": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "gmail.com": "imap.gmail.com",
    "outlook.com": "imap-mail.outlook.com",
    "hotmail.com": "imap-mail.outlook.com",
}

# Кандидаты IMAP серверов для Firstmail и Notletters кастомных доменов
CANDIDATE_IMAP_HOSTS = [
    "imap.firstmail.ltd",
    "imap.notletters.com",
    "mail.notletters.com",
]

DEFAULT_SUPERCELL_DELIVERY_TEMPLATE = (
    "⚡ <b>Данные вашего аккаунта {game_name}:</b>\n"
    "📦 Товар: <b>{lot_name}</b>\n\n"
    "📧 <b>Supercell ID (Почта):</b> <code>{email}</code>\n"
    "🔑 <b>Пароль от почты:</b> <code>{password}</code>\n"
    "{stats_block}"
    "{recovery_codes_block}"
    "{mail_access_block}"
    "\n{comment_block}"
    "📱 <b>Инструкция по входу в Supercell ID:</b>\n"
    "1. Откройте {game_name} и зайдите в Настройки ⚙️\n"
    "2. Нажмите «Подключить Supercell ID» -> «Войти»\n"
    "3. Введите указанную почту: <code>{email}</code>\n"
    "4. В чат напишите <code>#код</code> или <code>#code</code> — бот автоматически зайдёт на почту и пришлёт вам 6-значный код подтверждения!\n\n"
    "🤝 <i>Спасибо за покупку! Пожалуйста, проверьте аккаунт и подтвердите получение заказа.</i>"
)


class SupercellStock(Base):
    """Склад аккаунтов Supercell с привязкой к конкретной игре."""
    __tablename__ = "supercell_stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    game_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # brawl_stars, clash_royale, clash_of_clans
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Автоопределенный проверенный IMAP сервер почты (firstmail, notletters и т.д.)
    imap_host: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Характеристики аккаунта
    trophies: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Кубки
    town_hall: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Уровень ТХ (для CoC)
    brawlers_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Бравлеры (для BS)
    recovery_codes_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)  # Коды восстановления Supercell ID
    has_mail_access: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # Доступ к почте (родная/авторег)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False, index=True)  # available, issued
    issued_deal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SupercellLotRule(Base):
    """Привязка лота Playerok к автовыдаче Supercell."""
    __tablename__ = "supercell_lot_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    game_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_title: Mapped[str] = mapped_column(String(256), nullable=False)
    delivery_template: Mapped[str] = mapped_column(Text, default=DEFAULT_SUPERCELL_DELIVERY_TEMPLATE, nullable=False)
    auto_sent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "lot_id", name="uq_supercell_lot_rule"),
    )


class SupercellDeal(Base):
    """История сделок по аккаунтам Supercell."""
    __tablename__ = "supercell_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Any] = mapped_column(
        ForeignKey("playerok_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("supercell_stocks.id", ondelete="CASCADE"), nullable=False
    )
    game_key: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    deal_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "deal_id", name="uq_supercell_deal"),
    )


# --- ПОЛУЧЕНИЕ КОДОВ ПОДТВЕРЖДЕНИЯ С ПОЧТЫ (FIRSTMAIL / NOTLETTERS / IMAP / WEB API) ---

def extract_supercell_code_from_text(content: str) -> str | None:
    """Извлекает 6-значный код подтверждения Supercell ID из темы или тела письма."""
    if not content:
        return None

    # 1. Приоритетный поиск кода в контексте Supercell
    supercell_pattern = re.search(
        r"(?:supercell|supercell id|код|code|вход|login|verification)[\s\S]{0,100}?\b([0-9]{6})\b",
        content,
        re.IGNORECASE,
    )
    if supercell_pattern:
        return supercell_pattern.group(1)

    # 2. Поиск любого 6-значного числового кода в письме
    codes = re.findall(r"\b([0-9]{6})\b", content)
    if codes:
        # Возвращаем первый найденный 6-значный код
        return codes[0]

    return None


def get_imap_host_candidates(email_address: str) -> list[str]:
    """
    Определяет упорядоченный список IMAP серверов для проверки.
    Правило: если домен неизвестный/кастомный, сначала ОБЯЗАТЕЛЬНО проверяется Firstmail (imap.firstmail.ltd),
    затем Notletters (imap.notletters.com), а затем доменные mx/imap адреса.
    """
    domain = email_address.split("@")[-1].lower() if "@" in email_address else ""
    hosts: list[str] = []

    # 1. Если домен явно известный в справочнике
    if domain in KNOWN_IMAP_DOMAINS:
        hosts.append(KNOWN_IMAP_DOMAINS[domain])

    # 2. Если в домене упоминаются ключевые слова Firstmail / eblanmail
    if any(k in domain for k in ("firstmail", "fmail", "eblan", "first", "fm")):
        if "imap.firstmail.ltd" not in hosts:
            hosts.append("imap.firstmail.ltd")

    # 3. Для любых незнакомых доменов ПЕРВЫМ делом ставим Firstmail!
    if "imap.firstmail.ltd" not in hosts:
        hosts.append("imap.firstmail.ltd")

    # 4. Если не подошел Firstmail — вторым пробуем Notletters
    if "imap.notletters.com" not in hosts:
        hosts.append("imap.notletters.com")

    # 5. Затем проверяем прямые imap.{domain} и mail.{domain}
    if domain:
        d_imap = f"imap.{domain}"
        d_mail = f"mail.{domain}"
        if d_imap not in hosts:
            hosts.append(d_imap)
        if d_mail not in hosts:
            hosts.append(d_mail)

    # 6. Резервные пулы
    for cand in CANDIDATE_IMAP_HOSTS:
        if cand not in hosts:
            hosts.append(cand)

    return hosts


def _test_imap_login_sync(email_address: str, password: str, host: str, timeout_sec: int = 5) -> bool:
    """Проверяет возможность авторизации на IMAP сервере."""
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(host, timeout=timeout_sec)
        res, _ = mail.login(email_address, password)
        return res == "OK"
    except Exception:
        return False
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


async def detect_and_verify_email_host(email_address: str, password: str) -> str | None:
    """
    При добавлении аккаунта на склад проверяет, на каком почтовом сервере работает email.
    Сначала тестирует Firstmail (imap.firstmail.ltd), если ошибка — пробует Notletters и другие.
    Возвращает работающий imap_host или None, если сервер пока недоступен.
    """
    candidates = get_imap_host_candidates(email_address)
    for host in candidates:
        ok = await asyncio.to_thread(_test_imap_login_sync, email_address, password, host, 5)
        if ok:
            logger.info("Email %s successfully verified on IMAP host: %s", email_address, host)
            return host
    return None


def _read_imap_sync(email_address: str, password: str, host: str, timeout_sec: int = 7) -> str | None:
    """Синхронно подключается к IMAP и читает последнее письмо от Supercell."""
    mail = None
    try:
        # Проверяем резолв DNS перед коннектом
        mail = imaplib.IMAP4_SSL(host, timeout=timeout_sec)
        mail.login(email_address, password)
        mail.select("INBOX")

        # Ищем письма
        status, messages = mail.search(None, "ALL")
        if status != "OK" or not messages[0]:
            return None

        msg_ids = messages[0].split()
        if not msg_ids:
            return None

        # Проверяем последние 5 писем (от новых к старым)
        for mid in reversed(msg_ids[-5:]):
            res, data = mail.fetch(mid, "(RFC822)")
            if res != "OK" or not data or not data[0]:
                continue

            raw_bytes = data[0][1]
            if not isinstance(raw_bytes, (bytes, bytearray)):
                continue

            msg = email_pkg.message_from_bytes(raw_bytes)
            subject = str(msg.get("Subject", "") or "")

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype in ("text/plain", "text/html"):
                        try:
                            payload = part.get_payload(decode=True)
                            if payload:
                                body += payload.decode("utf-8", errors="ignore")
                        except Exception:
                            pass
            else:
                try:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="ignore")
                except Exception:
                    pass

            full_text = f"{subject}\n{body}"
            code = extract_supercell_code_from_text(full_text)
            if code:
                return code

        return None
    except Exception as exc:
        logger.debug("IMAP error on %s for %s: %s", host, email_address, exc)
        return None
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


async def fetch_supercell_code_via_imap(
    email_address: str, password: str, pinned_host: str | None = None, timeout_sec: int = 15
) -> str | None:
    """Асинхронно перебирает возможные IMAP хосты (или использует проверенный pinned_host) и извлекает код подтверждения Supercell ID."""
    hosts = [pinned_host] if pinned_host else get_imap_host_candidates(email_address)

    for host in hosts:
        code = await asyncio.to_thread(_read_imap_sync, email_address, password, host, 7)
        if code:
            logger.info("Found Supercell code %s via IMAP host %s for %s", code, host, email_address)
            return code

    return None


async def fetch_supercell_code_via_api(email_address: str, password: str) -> str | None:
    """Пытается получить код через HTTP API популярных временных почт (Firstmail/Notletters), если IMAP недоступен."""
    domain = email_address.split("@")[-1].lower() if "@" in email_address else ""

    # Пробуем Firstmail Web API
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            # Firstmail endpoint проверки почты
            fm_url = "https://api.firstmail.ltd/v1/mail/list"
            r = await client.get(
                fm_url,
                params={"username": email_address, "password": password},
                headers={"Accept": "application/json"},
            )
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") or data.get("messages") or []
                for it in items:
                    txt = f"{it.get('subject', '')} {it.get('body', '')} {it.get('text', '')}"
                    code = extract_supercell_code_from_text(txt)
                    if code:
                        return code
    except Exception as exc:
        logger.debug("Firstmail API check error: %s", exc)

    return None


async def fetch_supercell_auth_code(
    email_address: str, password: str, pinned_host: str | None = None
) -> str | None:
    """Универсальная функция: сначала пробует IMAP (проверенный хост или автоопределение Firstmail->Notletters), затем Web API."""
    # 1. IMAP проверка
    code = await fetch_supercell_code_via_imap(email_address, password, pinned_host=pinned_host)
    if code:
        return code

    # 2. Web API проверка
    code = await fetch_supercell_code_via_api(email_address, password)
    if code:
        return code

    return None


def format_supercell_delivery_message(
    template: str,
    game_key: str,
    lot_name: str,
    email: str,
    password: str,
    trophies: int | None = None,
    town_hall: int | None = None,
    brawlers_count: int | None = None,
    recovery_codes: str | None = None,
    has_mail_access: bool = True,
    comment: str | None = None,
) -> str:
    """Форматирует сообщение покупателю с подстановкой параметров аккаунта Supercell."""
    tmpl = template or DEFAULT_SUPERCELL_DELIVERY_TEMPLATE
    game_name = SUPERCELL_GAMES.get(game_key, game_key.replace("_", " ").title())

    stats_parts = []
    if trophies is not None:
        stats_parts.append(f"🏆 <b>Кубки/Трофеи:</b> <code>{trophies:,}</code>")
    if town_hall is not None:
        stats_parts.append(f"🏰 <b>Уровень Ратуши (ТХ):</b> <code>{town_hall}</code>")
    if brawlers_count is not None:
        stats_parts.append(f"🥊 <b>Бойцы (Бравлеры):</b> <code>{brawlers_count}</code>")
    stats_block = ("\n".join(stats_parts) + "\n") if stats_parts else ""

    recovery_codes_block = (
        f"🔐 <b>Коды восстановления Supercell ID:</b>\n<code>{html.escape(recovery_codes)}</code>\n"
        if recovery_codes else ""
    )

    mail_access_block = (
        f"📬 <b>Автоматическое получение кодов:</b> Включено (Firstmail / Notletters / IMAP)\n"
    )

    comment_block = f"📝 <b>Инструкция продавца:</b>\n{html.escape(comment)}\n" if comment else ""

    text = tmpl
    text = text.replace("{game_name}", html.escape(game_name))
    text = text.replace("{lot_name}", html.escape(lot_name))
    text = text.replace("{email}", html.escape(email))
    text = text.replace("{password}", html.escape(password))
    text = text.replace("{stats_block}", stats_block)
    text = text.replace("{recovery_codes_block}", recovery_codes_block)
    text = text.replace("{mail_access_block}", mail_access_block)
    text = text.replace("{comment_block}", comment_block)

    return text.strip()


def parse_supercell_bulk_lines(text: str) -> list[dict[str, Any]]:
    """Парсит строки формата email:password[:recovery_codes:comment] или с разделителем пробел/таб/точка с запятой."""
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts: list[str]
        if ":" in line:
            parts = [p.strip() for p in line.split(":")]
        elif ";" in line:
            parts = [p.strip() for p in line.split(";")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = [p.strip() for p in line.split()]

        if len(parts) >= 2:
            email = parts[0]
            password = parts[1]
            recovery_codes = parts[2] if len(parts) > 2 else None
            comment = parts[3] if len(parts) > 3 else None
            results.append({
                "email": email,
                "password": password,
                "recovery_codes": recovery_codes,
                "comment": comment,
            })
    return results
