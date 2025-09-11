from sqlalchemy.orm import declarative_base
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    create_engine,
    # UniqueConstraint,
    event,
    # text,
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


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


# def make_engine(DATABASE_URL):
#     engine = create_engine(
#         DATABASE_URL,
#         future=True,
#         connect_args={
#             "check_same_thread": False
#         } if DATABASE_URL.startswith("sqlite") else {},
#         pool_pre_ping=True,
#     )
#
#     # Enable WAL + busy timeout for SQLite
#     if DATABASE_URL.startswith("sqlite"):
#         @event.listens_for(engine, "connect")
#         def sqlite_pragmas(dbapi_connection, connection_record):
#             cursor = dbapi_connection.cursor()
#             cursor.execute("PRAGMA journal_mode=WAL;")
#             cursor.execute("PRAGMA busy_timeout=5000;")
#             cursor.execute("PRAGMA synchronous=NORMAL;")
#             cursor.close()
#
#     Base.metadata.create_all(bind=engine)
#     return engine


def make_async_engine(database_url: str):
    # swap to aiosqlite
    if database_url.startswith("sqlite://"):
        database_url = database_url.replace("sqlite://", "sqlite+aiosqlite://", 1)

    engine = create_async_engine(database_url, future=True, pool_pre_ping=True)

    # SQLite PRAGMAs (attach to the sync_engine behind the async engine)
    if database_url.startswith("sqlite+aiosqlite://"):
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.close()

    SessionAsync = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,  # avoid objects expiring after commit
        autoflush=False,         # keep flush manual
    )

    return engine, SessionAsync
