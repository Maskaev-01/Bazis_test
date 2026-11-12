import secrets
import uuid
from typing import Annotated
from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.config import settings
from app.db import Session
from app.models import Outbox, Payment
from app.schemas import PaymentAccepted, PaymentCreate, PaymentDetail


def authenticate(api_key: Annotated[str | None, Header(alias="X-API-Key")] = None):
    if api_key is None or not secrets.compare_digest(api_key.encode("utf-8"), settings.api_key.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid API key")


app = FastAPI(title="Payments API", dependencies=[Depends(authenticate)], docs_url=None, redoc_url=None, openapi_url=None)


def accepted(payment: Payment) -> PaymentAccepted:
    return PaymentAccepted(payment_id=payment.id, status=payment.status, created_at=payment.created_at)


@app.post("/api/v1/payments", status_code=status.HTTP_202_ACCEPTED, response_model=PaymentAccepted)
async def create_payment(body: PaymentCreate, key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]):
    payment_id = uuid.uuid4()
    values = dict(id=payment_id, amount=body.amount, currency=body.currency,
                  description=body.description, metadata_json=body.metadata, status="pending",
                  idempotency_key=key, webhook_url=str(body.webhook_url))
    async with Session.begin() as session:
        inserted = (await session.execute(insert(Payment).values(**values).on_conflict_do_nothing(index_elements=[Payment.idempotency_key]).returning(Payment.id))).scalar_one_or_none()
        if inserted is not None:
            session.add(Outbox(id=uuid.uuid4(), payment_id=payment_id, payload={"payment_id": str(payment_id)}))
            payment = await session.get(Payment, payment_id)
        else:
            payment = (await session.execute(select(Payment).where(Payment.idempotency_key == key))).scalar_one()
            if (payment.amount != body.amount or payment.currency != body.currency or payment.description != body.description
                or payment.metadata_json != body.metadata or payment.webhook_url != str(body.webhook_url)):
                raise HTTPException(status_code=409, detail="Idempotency key already used with a different request")
        return accepted(payment)


@app.get("/api/v1/payments/{payment_id}", response_model=PaymentDetail)
async def get_payment(payment_id: uuid.UUID):
    async with Session() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return PaymentDetail(**accepted(payment).model_dump(), amount=payment.amount, currency=payment.currency,
                             description=payment.description, metadata=payment.metadata_json,
                             webhook_url=payment.webhook_url, processed_at=payment.processed_at)
