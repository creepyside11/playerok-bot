from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .crypto import SecretCipher
from .models import DeliveryRule, DeliveryStock


async def save_item_delivery(
    db: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    account_id: UUID,
    item_id: str,
    message: str,
) -> None:
    async with db() as session:
        rule = await session.scalar(
            select(DeliveryRule).where(
                DeliveryRule.account_id == account_id, DeliveryRule.item_id == item_id
            )
        )
        if rule is None:
            rule = DeliveryRule(account_id=account_id, item_id=item_id)
            session.add(rule)
        else:
            await session.execute(
                delete(DeliveryStock).where(DeliveryStock.rule_id == rule.id)
            )

        rule.enabled = True
        rule.mode = "message"
        rule.message_template = "{stock}"
        await session.flush()
        session.add(DeliveryStock(rule_id=rule.id, payload_encrypted=cipher.encrypt(message)))
        await session.commit()
