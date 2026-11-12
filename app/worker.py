import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone

import httpx
from faststream import FastStream
from faststream.exceptions import RejectMessage
from faststream.rabbit import RabbitBroker, RabbitExchange, RabbitQueue
from sqlalchemy import select

from app.config import settings
from app.db import Session
from app.models import Outbox, Payment

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
broker = RabbitBroker(settings.rabbit_url, publisher_confirms=True, on_return_raises=True)
exchange = RabbitExchange("payments", durable=True)
dlx = RabbitExchange("payments.dlx", durable=True)
queue = RabbitQueue("payments.new", durable=True, routing_key="payments.new", arguments={"x-dead-letter-exchange": "payments.dlx", "x-dead-letter-routing-key": "payments.dead"})
dlq = RabbitQueue("payments.dead", durable=True, routing_key="payments.dead")
app = FastStream(broker)


async def publish_outbox():
    while True:
        try:
            async with Session.begin() as session:
                rows = (await session.execute(select(Outbox).where(Outbox.published_at.is_(None)).order_by(Outbox.created_at).with_for_update(skip_locked=True).limit(50))).scalars().all()
                for row in rows:
                    await broker.publish(row.payload, exchange=exchange, routing_key="payments.new", persist=True, mandatory=True, message_id=str(row.id))
                    row.published_at = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Outbox publishing failed; will retry")
        await asyncio.sleep(settings.outbox_poll_seconds)


@app.on_startup
async def declare_dead_letter_topology():
    await broker.connect()
    dead_exchange = await broker.declare_exchange(dlx)
    dead_queue = await broker.declare_queue(dlq)
    await dead_queue.bind(dead_exchange, routing_key="payments.dead")


@app.after_startup
async def startup():
    global outbox_task
    outbox_task = asyncio.create_task(publish_outbox())


@app.on_shutdown
async def shutdown():
    outbox_task.cancel()
    await asyncio.gather(outbox_task, return_exceptions=True)


async def process_payment(payment_id: uuid.UUID):
    async with Session.begin() as session:
        payment = (await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())).scalar_one()
        if payment.status == "pending":
            await asyncio.sleep(random.uniform(2, 5))
            payment.status = "succeeded" if random.random() < 0.9 else "failed"
            payment.processed_at = datetime.now(timezone.utc)
        payload = {"payment_id": str(payment.id), "status": payment.status,
                   "amount": str(payment.amount), "currency": payment.currency,
                   "processed_at": payment.processed_at.isoformat()}
        webhook_url = payment.webhook_url
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, follow_redirects=False) as client:
        response = await client.post(webhook_url, json=payload, headers={"Idempotency-Key": str(payment_id)})
        response.raise_for_status()


@broker.subscriber(queue, exchange=exchange, retry=False)
async def consume(message: dict):
    for attempt in range(1, 4):
        try:
            await process_payment(uuid.UUID(message["payment_id"]))
            return
        except Exception:
            log.exception("Payment processing failed (attempt %s/3)", attempt)
            if attempt == 3:
                raise RejectMessage(requeue=False)
            await asyncio.sleep(settings.retry_base_seconds * (2 ** (attempt - 1)))


if __name__ == "__main__":
    asyncio.run(app.run())
