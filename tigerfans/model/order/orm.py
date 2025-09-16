from sqlalchemy.orm import declarative_base
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
)


Base = declarative_base()


# ----------------------------
# ORM models
# ----------------------------
class Order(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    tb_transfer_id = Column(String, nullable=False, unique=True)
    goodie_tb_transfer_id = Column(String, nullable=False, unique=True)
    try_goodie = Column(Boolean, nullable=False)
    cls = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)  # cents
    currency = Column(String, nullable=False, default="eur")
    customer_email = Column(String, nullable=False)

    # PENDING | PAID | FAILED | CANCELED | REFUNDED
    status = Column(String, nullable=False, default="PENDING")
    created_at = Column(Float, nullable=False)
    paid_at = Column(Float, nullable=True)

    ticket_code = Column(String, nullable=True, unique=True)
    got_goodie = Column(Boolean, nullable=False, default=False)

    # for Stripe; unused in Mock
    payment_intent_id = Column(String, nullable=True)
    # for Stripe; unused in Mock
    charge_id = Column(String, nullable=True)
