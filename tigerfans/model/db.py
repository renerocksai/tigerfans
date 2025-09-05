from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    create_engine,
    UniqueConstraint,
    event,
    text,
)


Base = declarative_base()


# ----------------------------
# ORM models
# ----------------------------
class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(String, primary_key=True)
    cls = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)
    # ACTIVE | EXPIRED | CANCELED | CONVERTED
    status = Column(String, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    reservation_id = Column(String, nullable=True)
    cls = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)  # cents
    currency = Column(String, nullable=False, default="eur")
    customer_email = Column(String, nullable=True)

    # PENDING | PAID | FAILED | CANCELED | REFUNDED
    status = Column(String, nullable=False, default="PENDING")
    created_at = Column(Float, nullable=False)
    paid_at = Column(Float, nullable=True)
    payment_session_id = Column(String, nullable=True)

    # for Stripe; unused in Mock
    payment_intent_id = Column(String, nullable=True)

    # for Stripe; unused in Mock
    charge_id = Column(String, nullable=True)

    tickets_csv = Column(String, nullable=True)  # comma-separated ticket codes
    got_goodie = Column(Boolean, nullable=False, default=False)


class PaymentSession(Base):
    __tablename__ = "payment_sessions"
    id = Column(String, primary_key=True)
    order_id = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String, nullable=False)
    created_at = Column(Float, nullable=False)


class WebhookEventSeen(Base):
    __tablename__ = "webhook_events_seen"
    idempotency_key = Column(String, primary_key=True)


class FulfillmentKey(Base):
    __tablename__ = "fulfillment_keys"
    key = Column(String, primary_key=True)  # order_id:session_id


class GoodiesCounter(Base):
    __tablename__ = "goodies_counter"
    cls = Column(String, primary_key=True)
    granted = Column(Integer, nullable=False, default=0)


def make_engine(DATABASE_URL):
    engine = create_engine(
        DATABASE_URL,
        future=True,
        connect_args={
            "check_same_thread": False
        } if DATABASE_URL.startswith("sqlite") else {},
        pool_pre_ping=True,
    )

    # Enable WAL + busy timeout for SQLite
    if DATABASE_URL.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.close()

    Base.metadata.create_all(bind=engine)
    return engine

