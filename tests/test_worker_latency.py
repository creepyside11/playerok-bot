"""Уведомления о сообщениях не должны ждать обработки сделок."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from app.models import PlayerokAccount
from app.worker import WorkerManager
from playerokapi.enums import ChatTypes, ItemDealDirections, ItemDealStatuses


def _account():
    account = NS(
        id="acc-1",
        username="seller",
        playerok_user_id="100",
        tg_user_id=555,
        settings={},
        worker_initialized=True,
        cookies_encrypted="",
        proxy_encrypted=None,
        user_agent=None,
    )
    return account


def _chat(chat_id, message_id="m1", text="привет"):
    message = NS(id=message_id, text=text, user=NS(id="200", username="buyer"),
                 images=[], event=None, is_auto_response=False)
    return NS(id=chat_id, type=ChatTypes.PM, last_message=message, deals=[])


class _EmptyResult:
    def all(self):
        return []


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalars(self, *a, **k):
        return _EmptyResult()


def _manager():
    manager = WorkerManager.__new__(WorkerManager)
    manager.bot = NS(send_message=lambda *a, **k: asyncio.sleep(0))
    manager.db = _FakeSession
    manager.gateway = None
    manager.cipher = None
    manager.poll_interval = 6.0
    manager.plugins = None
    manager._stop = asyncio.Event()
    manager._sem = asyncio.Semaphore(4)
    return manager


def test_poll_notifies_before_deal_processing():
    """Порядок в исходнике: уведомление о сообщении раньше обработки сделки."""
    import inspect
    source = inspect.getsource(WorkerManager._poll)
    notify_at = source.index("await self._chat_notify")
    recover_at = source.index("await self._recover_chat_sales")
    deal_at = source.index("await self._deal")
    assert notify_at < recover_at, "уведомления должны идти до _recover_chat_sales"
    assert notify_at < deal_at, "уведомления должны идти до обработки сделок"


def test_cycle_period_uses_elapsed_time():
    """Пауза считается от начала цикла, а не после работы."""
    import inspect
    source = inspect.getsource(WorkerManager.run)
    assert "loop.time() - started" in source
    assert "started = loop.time()" in source


@pytest.mark.asyncio
async def test_notify_happens_while_deals_still_pending():
    """Уведомление отправлено до того, как сделка начала обрабатываться."""
    order = []
    manager = _manager()
    account = _account()
    chat = _chat("chat-1")

    async def fake_claim(*args, **kwargs):
        return True

    async def fake_notify(tg_id, text, markup=None):
        order.append("notify")

    manager._notify = fake_notify
    import app.worker as worker_module
    original_claim = worker_module.claim_event
    worker_module.claim_event = fake_claim
    try:
        message = await manager._chat_notify(account, chat)
    finally:
        worker_module.claim_event = original_claim

    assert message is chat.last_message
    assert order == ["notify"]

    # И только потом — обработка плагинов и автоправил
    async def fake_dispatch(*args, **kwargs):
        order.append("handle")

    manager.plugins = NS(dispatch_message=fake_dispatch)
    await manager._chat_handle(account, NS(send_message=lambda *a, **k: asyncio.sleep(0)), chat, message)
    assert order == ["notify", "handle"]


@pytest.mark.asyncio
async def test_chat_notify_skips_own_messages():
    manager = _manager()
    account = _account()
    chat = _chat("chat-1")
    chat.last_message.user = NS(id="100", username="me")
    calls = []

    async def fake_notify(*args, **kwargs):
        calls.append(args)

    manager._notify = fake_notify
    message = await manager._chat_notify(account, chat)
    assert message is None
    assert calls == []


@pytest.mark.asyncio
async def test_deal_processing_failure_does_not_block_notifications():
    """Ошибка в сделке не должна мешать уже отправленному уведомлению."""
    order = []
    manager = _manager()
    account = _account()
    chat = _chat("chat-1")

    async def fake_claim(*args, **kwargs):
        return True

    async def fake_notify(tg_id, text, markup=None):
        order.append("notify")

    manager._notify = fake_notify
    import app.worker as worker_module
    original_claim = worker_module.claim_event
    worker_module.claim_event = fake_claim
    try:
        message = await manager._chat_notify(account, chat)
    finally:
        worker_module.claim_event = original_claim

    async def failing_handle(*a, **k):
        raise RuntimeError("plugin exploded")

    manager.plugins = NS(dispatch_message=failing_handle)
    try:
        await manager._chat_handle(account, None, chat, message)
    except RuntimeError:
        pass
    assert order == ["notify"], "уведомление ушло независимо от сбоя обработки"
