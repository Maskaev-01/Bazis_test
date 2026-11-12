import asyncio
from unittest.mock import AsyncMock
import httpx
import pytest
from faststream.exceptions import RejectMessage
from fastapi.testclient import TestClient
from app.api import app
from app import worker
from app.schemas import PaymentCreate
from pydantic import ValidationError


def test_authentication_and_validation():
    client = TestClient(app)
    assert client.get("/api/v1/payments/00000000-0000-0000-0000-000000000000").status_code == 401
    response = client.post("/api/v1/payments", headers={"X-API-Key": "change-me"}, json={})
    assert response.status_code == 422
    with pytest.raises(ValidationError):
        PaymentCreate(amount="0", currency="RUB", description="", webhook_url="http://example.com")
    with pytest.raises(ValidationError):
        PaymentCreate(amount="1.001", currency="RUB", description="", webhook_url="http://example.com")


@pytest.mark.asyncio
async def test_retry_then_success(monkeypatch):
    process = AsyncMock(side_effect=[httpx.ConnectError("down"), None])
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "process_payment", process)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await worker.consume({"payment_id": "00000000-0000-0000-0000-000000000001"})
    assert process.await_count == 2
    sleep.assert_awaited_once_with(worker.settings.retry_base_seconds)


@pytest.mark.asyncio
async def test_failed_message_rejected_to_dlq(monkeypatch):
    process = AsyncMock(side_effect=RuntimeError("failed"))
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "process_payment", process)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(RejectMessage):
        await worker.consume({"payment_id": "00000000-0000-0000-0000-000000000001"})
    assert process.await_count == 3
    assert [c.args[0] for c in sleep.await_args_list] == [worker.settings.retry_base_seconds, 2 * worker.settings.retry_base_seconds]



def test_non_ascii_api_key_is_rejected():
    from app.api import authenticate
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        authenticate("ключ")
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_completed_payment_is_not_processed_again(monkeypatch):
    from datetime import datetime, timezone
    from decimal import Decimal
    from types import SimpleNamespace
    payment = SimpleNamespace(id=__import__("uuid").uuid4(), status="succeeded", amount=Decimal("10.00"), currency="RUB", processed_at=datetime.now(timezone.utc), webhook_url="http://example.com/hook")
    result = SimpleNamespace(scalar_one=lambda: payment)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    class Transaction:
        async def __aenter__(self): return session
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(worker, "Session", SimpleNamespace(begin=lambda: Transaction()))
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    response = httpx.Response(200, request=httpx.Request("POST", payment.webhook_url))
    post = AsyncMock(return_value=response)
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return await post(*args, **kwargs)
    monkeypatch.setattr(worker.httpx, "AsyncClient", Client)
    await worker.process_payment(payment.id)
    sleep.assert_not_awaited()
    assert post.await_args.kwargs["json"]["status"] == "succeeded"
    assert post.await_args.kwargs["headers"]["Idempotency-Key"] == str(payment.id)
