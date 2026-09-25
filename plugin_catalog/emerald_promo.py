from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from sqlalchemy import select

from app.emerald_promo_manager import (
    ACTIVATION_URL,
    EmeraldClient,
    EmeraldPromoIssue,
    EmeraldPromoLotRule,
    EmeraldPromoSetting,
    TELEGRAPH_DOCS_URL,
    fetch_models_text,
    format_tokens,
    free_message,
    promo_prefix,
    review_message,
    sale_message,
)
from app.handlers import svc


logger = logging.getLogger("plugin.emerald_promo")

PLUGIN_META = {
    "id": "emerald_promo",
    "name": "Emerald Promo",
    "version": "1.1.0",
    "author": "Playerok Bot",
    "description": (
        "Автоматическая выдача API-ключей и промокодов EmeraldAI для gateway emeraldai.beer. "
        "Включает выбор лота кнопкой, выдачу по оплате, бонус за отзыв 5★ и команду #баланс."
    ),
    "settings": {},
}


async def on_action(ctx: Any, action: str, payload: dict[str, Any]) -> None:
    # Handle manual / test connections or trigger settings
    if action == "test_connection":
        account = ctx.account
        async with ctx.db() as session:
            setting = await session.scalar(
                select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == account.id)
            )
        if not setting or not setting.api_token_enc:
            await ctx.notify("❌ Emerald Promo: API-токен не настроен в меню плагина.")
            return
        token = svc().cipher.decrypt(setting.api_token_enc)
        if not token:
            await ctx.notify("❌ Emerald Promo: невозможно расшифровать токен.")
            return
        try:
            client = EmeraldClient(setting.api_base_url, token)
            data = await asyncio.to_thread(client.get_account)
            bal = int(data.get("balance_tokens") or 0)
            await ctx.notify(f"✅ Emerald API отвечает!\nБаланс токенов: <b>{format_tokens(bal)}</b>")
        except Exception as exc:
            await ctx.notify(f"❌ Ошибка Emerald API: {str(exc)[:500]}")


async def on_deal(ctx: Any, deal: Any) -> None:
    # Called on new deal / order
    account = ctx.account
    item = getattr(deal, "item", None)
    item_id = str(getattr(item, "id", "") or "")
    if not item_id:
        return

    async with ctx.db() as session:
        setting = await session.scalar(
            select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == account.id)
        )
        if not setting or not setting.api_token_enc:
            return

        rule = await session.scalar(
            select(EmeraldPromoLotRule).where(
                EmeraldPromoLotRule.account_id == account.id,
                EmeraldPromoLotRule.lot_id == item_id,
                EmeraldPromoLotRule.enabled.is_(True),
            )
        )
        if not rule:
            return

        # Check existing issue
        deal_id = str(getattr(deal, "id", ""))
        existing = await session.scalar(
            select(EmeraldPromoIssue).where(
                EmeraldPromoIssue.account_id == account.id,
                EmeraldPromoIssue.kind == "sale",
                EmeraldPromoIssue.source_id == deal_id,
            )
        )
        if existing and existing.status == "sent":
            return

        buyer = getattr(deal, "user", None)
        buyer_id = str(getattr(buyer, "id", "") or "")
        chat = getattr(deal, "chat", None)
        chat_id = str(getattr(chat, "id", "") or "")
        if not chat_id:
            return

        token = svc().cipher.decrypt(setting.api_token_enc)
        if not token:
            await ctx.notify("⚠️ Emerald Promo: ошибка дешифрования токена продавца.")
            return

        token_amount = int(rule.tokens_per_unit)
        prefix = promo_prefix(account.id, "sale", deal_id)

        if not existing:
            issue = EmeraldPromoIssue(
                account_id=account.id,
                kind="sale",
                source_id=deal_id,
                buyer_id=buyer_id,
                order_id=deal_id,
                chat_id=chat_id,
                chat_name=getattr(buyer, "username", None),
                rule_id=rule.id,
                lot_title=rule.lot_title,
                purchased_units=1,
                token_amount=token_amount,
                review_bonus_enabled=rule.review_bonus_enabled,
                review_bonus_tokens=rule.review_bonus_tokens,
                prefix=prefix,
                status="creating",
            )
            session.add(issue)
            await session.commit()
            await session.refresh(issue)
        else:
            issue = existing

    # Generate key or promo code
    client = EmeraldClient(setting.api_base_url, token)
    is_key = setting.issue_type == "api_key"
    try:
        if is_key:
            name = f"PO SALE {deal_id}"
            key_data = await asyncio.to_thread(client.create_api_key, token_amount, name, setting.key_target)
            code = str(key_data.get("api_key") or key_data.get("prefix") or "")
            promo_id = str(key_data.get("id") or "")
        else:
            promo_data = await asyncio.to_thread(client.create_promo_code, token_amount, prefix)
            code = str(promo_data.get("code") or "")
            promo_id = str(promo_data.get("id") or "")

        if not code:
            raise RuntimeError("Emerald API не вернул код/ключ")

        msg_text = sale_message(
            code,
            token_amount,
            1,
            rule.review_bonus_enabled,
            rule.review_bonus_tokens,
            is_key=is_key,
        )

        # Send to buyer in Playerok chat
        await asyncio.to_thread(ctx.client.send_message, chat_id, msg_text)

        async with ctx.db() as session:
            db_issue = await session.get(EmeraldPromoIssue, issue.id)
            if db_issue:
                db_issue.promo_code = code
                db_issue.api_promo_id = promo_id
                db_issue.status = "sent"
                await session.commit()

        await ctx.notify(
            f"💎 <b>Emerald Promo: выдача по сделке #{deal_id[-6:]}</b>\n"
            f"Товар: <b>{html.escape(rule.lot_title)}</b>\n"
            f"Выдано: <code>{html.escape(code)}</code> ({format_tokens(token_amount)} токенов)"
        )
    except Exception as exc:
        logger.exception("Emerald issue failed")
        async with ctx.db() as session:
            db_issue = await session.get(EmeraldPromoIssue, issue.id)
            if db_issue:
                db_issue.status = "failed"
                db_issue.error_text = str(exc)[:500]
                await session.commit()
        await ctx.notify(f"⚠️ <b>Emerald Promo: сбой выдачи</b> по сделке #{deal_id[-6:]}:\n{html.escape(str(exc)[:400])}")


async def on_review(ctx: Any, review: Any) -> None:
    # Bonus for 5 stars review
    rating = int(getattr(review, "rating", 0) or 0)
    if rating != 5:
        return

    deal = getattr(review, "deal", None)
    if not deal:
        return
    deal_id = str(getattr(deal, "id", "") or "")
    if not deal_id:
        return

    account = ctx.account
    async with ctx.db() as session:
        setting = await session.scalar(
            select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == account.id)
        )
        if not setting or not setting.api_token_enc:
            return

        sale_issue = await session.scalar(
            select(EmeraldPromoIssue).where(
                EmeraldPromoIssue.account_id == account.id,
                EmeraldPromoIssue.kind == "sale",
                EmeraldPromoIssue.source_id == deal_id,
            )
        )
        if (
            not sale_issue
            or not sale_issue.review_bonus_enabled
            or not sale_issue.review_bonus_tokens
            or sale_issue.review_bonus_tokens < 10000
        ):
            return

        existing_review_issue = await session.scalar(
            select(EmeraldPromoIssue).where(
                EmeraldPromoIssue.account_id == account.id,
                EmeraldPromoIssue.kind == "review",
                EmeraldPromoIssue.source_id == deal_id,
            )
        )
        if existing_review_issue and existing_review_issue.status == "sent":
            return

        token = svc().cipher.decrypt(setting.api_token_enc)
        if not token:
            return

        token_amount = int(sale_issue.review_bonus_tokens)
        prefix = promo_prefix(account.id, "review", deal_id)

        if not existing_review_issue:
            issue = EmeraldPromoIssue(
                account_id=account.id,
                kind="review",
                source_id=deal_id,
                buyer_id=sale_issue.buyer_id,
                order_id=deal_id,
                chat_id=sale_issue.chat_id,
                chat_name=sale_issue.chat_name,
                rule_id=sale_issue.rule_id,
                lot_title=sale_issue.lot_title,
                purchased_units=1,
                token_amount=token_amount,
                prefix=prefix,
                status="creating",
            )
            session.add(issue)
            await session.commit()
            await session.refresh(issue)
        else:
            issue = existing_review_issue

    client = EmeraldClient(setting.api_base_url, token)
    is_key = setting.issue_type == "api_key"
    try:
        if is_key:
            name = f"PO REVIEW {deal_id}"
            key_data = await asyncio.to_thread(client.create_api_key, token_amount, name, setting.key_target)
            code = str(key_data.get("api_key") or key_data.get("prefix") or "")
            promo_id = str(key_data.get("id") or "")
        else:
            promo_data = await asyncio.to_thread(client.create_promo_code, token_amount, prefix)
            code = str(promo_data.get("code") or "")
            promo_id = str(promo_data.get("id") or "")

        msg_text = review_message(code, token_amount, is_key=is_key)
        await asyncio.to_thread(ctx.client.send_message, sale_issue.chat_id, msg_text)

        async with ctx.db() as session:
            db_issue = await session.get(EmeraldPromoIssue, issue.id)
            if db_issue:
                db_issue.promo_code = code
                db_issue.api_promo_id = promo_id
                db_issue.status = "sent"
                await session.commit()

        await ctx.notify(
            f"⭐ <b>Emerald Promo: бонус за отзыв 5★ по сделке #{deal_id[-6:]}</b>\n"
            f"Выдано: <code>{html.escape(code)}</code> ({format_tokens(token_amount)} токенов)"
        )
    except Exception as exc:
        logger.exception("Review bonus issue failed")


async def on_message(ctx: Any, chat: Any, message: Any) -> None:
    text = (getattr(message, "text", "") or "").strip().lower()
    if not text:
        return
    chat_id = str(getattr(chat, "id", "") or "")
    if not chat_id:
        return

    account = ctx.account
    async with ctx.db() as session:
        setting = await session.scalar(
            select(EmeraldPromoSetting).where(EmeraldPromoSetting.account_id == account.id)
        )
        if not setting or not setting.api_token_enc:
            return

    # Check for #free
    if text == "#free":
        if not setting.free_enabled:
            return
        sender = getattr(message, "user", None)
        buyer_id = str(getattr(sender, "id", "") or "")
        if not buyer_id:
            return

        async with ctx.db() as session:
            existing = await session.scalar(
                select(EmeraldPromoIssue).where(
                    EmeraldPromoIssue.account_id == account.id,
                    EmeraldPromoIssue.kind == "free",
                    EmeraldPromoIssue.source_id == buyer_id,
                )
            )
            if existing and existing.status == "sent" and existing.promo_code:
                # Resend existing
                is_k = existing.promo_code.startswith("sk-em-") or setting.issue_type == "api_key"
                msg = free_message(existing.promo_code, existing.token_amount, repeated=True, is_key=is_k)
                await asyncio.to_thread(ctx.client.send_message, chat_id, msg)
                return

            token = svc().cipher.decrypt(setting.api_token_enc)
            if not token:
                return

            token_amount = setting.free_token_amount
            prefix = promo_prefix(account.id, "free", buyer_id)

            if not existing:
                issue = EmeraldPromoIssue(
                    account_id=account.id,
                    kind="free",
                    source_id=buyer_id,
                    buyer_id=buyer_id,
                    chat_id=chat_id,
                    chat_name=getattr(sender, "username", None),
                    token_amount=token_amount,
                    prefix=prefix,
                    status="creating",
                )
                session.add(issue)
                await session.commit()
                await session.refresh(issue)
            else:
                issue = existing

        client = EmeraldClient(setting.api_base_url, token)
        is_key = setting.issue_type == "api_key"
        try:
            if is_key:
                name = f"PO FREE {buyer_id}"
                key_data = await asyncio.to_thread(client.create_api_key, token_amount, name, setting.key_target)
                code = str(key_data.get("api_key") or key_data.get("prefix") or "")
                promo_id = str(key_data.get("id") or "")
            else:
                promo_data = await asyncio.to_thread(client.create_promo_code, token_amount, prefix)
                code = str(promo_data.get("code") or "")
                promo_id = str(promo_data.get("id") or "")

            msg = free_message(code, token_amount, repeated=False, is_key=is_key)
            await asyncio.to_thread(ctx.client.send_message, chat_id, msg)

            async with ctx.db() as session:
                db_issue = await session.get(EmeraldPromoIssue, issue.id)
                if db_issue:
                    db_issue.promo_code = code
                    db_issue.api_promo_id = promo_id
                    db_issue.status = "sent"
                    await session.commit()

            await ctx.notify(f"🎁 <b>Emerald Promo: выдан #free покупателю {getattr(sender, 'username', '—')}</b> ({format_tokens(token_amount)} токенов)")
        except Exception as exc:
            logger.exception("Free issue failed")

    # Check for #баланс
    elif text.startswith("#баланс"):
        # Look up last key issued to this chat
        async with ctx.db() as session:
            last_issue = await session.scalar(
                select(EmeraldPromoIssue).where(
                    EmeraldPromoIssue.account_id == account.id,
                    EmeraldPromoIssue.chat_id == chat_id,
                    EmeraldPromoIssue.status == "sent",
                ).order_by(EmeraldPromoIssue.created_at.desc())
            )
        if not last_issue or not last_issue.promo_code:
            await asyncio.to_thread(ctx.client.send_message, chat_id, "ℹ️ Для этого чата не найден выданный API-ключ.")
            return

        key = last_issue.promo_code.strip()
        if not key.startswith("sk-em-"):
            await asyncio.to_thread(
                ctx.client.send_message,
                chat_id,
                f"ℹ️ Проверка баланса доступна для API-ключей. Инструкция по промокодам в Telegraph:\n{TELEGRAPH_DOCS_URL}",
            )
            return

        try:
            res = requests.get(
                "https://www.emeraldai.beer/v1/account",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=15,
            )
            data = res.json()
            account_obj = data.get("data") if isinstance(data, dict) else {}
            balance_val = account_obj.get("balance_tokens") or account_obj.get("tokens") or 0
            await asyncio.to_thread(
                ctx.client.send_message,
                chat_id,
                f"💳 Информация о ключе:\n"
                f"🔑 Ключ: {key[:8]}...{key[-4:]}\n"
                f"🪙 Текущий остаток: {format_tokens(int(balance_val))} токенов\n\n"
                f"📋 Список моделей и статус: #модели\n"
                f"📖 Документация в Telegraph:\n{TELEGRAPH_DOCS_URL}",
            )
        except Exception as exc:
            await asyncio.to_thread(ctx.client.send_message, chat_id, "⚠️ Не удалось получить баланс ключа.")

    # Check for #модели / #models
    elif text.strip().lower() in ("#модели", "#models", "#модель", "#model"):
        models_text = await asyncio.to_thread(fetch_models_text, TELEGRAPH_DOCS_URL)
        await asyncio.to_thread(ctx.client.send_message, chat_id, models_text)

    # Check for #документация / #доки / #docs
    elif text.strip().lower() in ("#документация", "#доки", "#инструкция", "#docs", "#руководство"):
        docs_text = (
            "📖 ОФИЦИАЛЬНАЯ ДОКУМЕНТАЦИЯ EMERALD AI:\n\n"
            f"{TELEGRAPH_DOCS_URL}\n\n"
            "⚙️ Base URL для клиентов: https://emeraldai.beer/v1\n\n"
            "💬 Команды в чате:\n"
            "• #модели — актуальный список моделей и их статус\n"
            "• #баланс — проверить остаток токенов ключа"
        )
        await asyncio.to_thread(ctx.client.send_message, chat_id, docs_text)
