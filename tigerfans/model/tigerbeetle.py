import os
import tigerbeetle as tb
from .db import Order, Session, GoodiesCounter

# Config
TicketAmount_Class_A = 1_000
TicketAmount_Class_B = 100_000
TicketAmount_first_n = 100

RestartCounter_max = 1_000_000

# Accounts
LedgerStats = 1000
LedgerTickets = 2000

RestartCounter_budget = tb.Account(id=1005, ledger=LedgerStats, code=10)
RestartCounter_spent = tb.Account(id=1000, ledger=LedgerStats, code=10,
    flags = tb.AccountFlags.CREDITS_MUST_NOT_EXCEED_DEBITS)

Class_A_first_n_budget = tb.Account(id=2115, ledger=LedgerTickets, code=20)
Class_A_first_n_spent = tb.Account(id=2110, ledger=LedgerTickets, code=20,
    flags = tb.AccountFlags.CREDITS_MUST_NOT_EXCEED_DEBITS)

Class_A_budget = tb.Account(id=2125, ledger=LedgerTickets, code=20)
Class_A_spent = tb.Account(id=2120, ledger=LedgerTickets, code=20,
    flags = tb.AccountFlags.CREDITS_MUST_NOT_EXCEED_DEBITS)


Class_B_first_n_budget = tb.Account(id=2215, ledger=LedgerTickets, code=20)
Class_B_first_n_spent = tb.Account(id=2210, ledger=LedgerTickets, code=20,
    flags = tb.AccountFlags.CREDITS_MUST_NOT_EXCEED_DEBITS)

Class_B_budget = tb.Account(id=2225, ledger=LedgerTickets, code=20)
Class_B_spent = tb.Account(id=2220, ledger=LedgerTickets, code=20,
    flags = tb.AccountFlags.CREDITS_MUST_NOT_EXCEED_DEBITS)


def create_accounts(client):
    account_errors = client.create_accounts([
        RestartCounter_spent,
        RestartCounter_budget,
        Class_A_first_n_spent,
        Class_A_first_n_budget,
        Class_A_spent,
        Class_A_budget,
        Class_B_first_n_spent,
        Class_B_first_n_budget,
        Class_B_spent,
        Class_B_budget,
    ])

    if account_errors:
        print(account_errors)
    assert len(account_errors) == 0
    print('✅ accounts created')
    return


def initial_transfers(client):
    transfer_errors = client.create_transfers([
        tb.Transfer(
            id=tb.id(),
            debit_account_id=RestartCounter_spent.id,
            credit_account_id=RestartCounter_budget.id,
            amount=RestartCounter_max,
            ledger=LedgerStats,
            code=1,
        ),

        tb.Transfer(
            id=tb.id(),
            debit_account_id=Class_A_first_n_spent.id,
            credit_account_id=Class_A_first_n_budget.id,
            amount=TicketAmount_first_n,
            ledger=LedgerTickets,
            code=1,
        ),
        tb.Transfer(
            id=tb.id(),
            debit_account_id=Class_B_first_n_spent.id,
            credit_account_id=Class_B_first_n_budget.id,
            amount=TicketAmount_first_n,
            ledger=LedgerTickets,
            code=1,
        ),

        tb.Transfer(
            id=tb.id(),
            debit_account_id=Class_A_spent.id,
            credit_account_id=Class_A_budget.id,
            amount=TicketAmount_Class_A,
            ledger=LedgerTickets,
            code=1,
        ),
        tb.Transfer(
            id=tb.id(),
            debit_account_id=Class_B_first_n_spent.id,
            credit_account_id=Class_B_first_n_budget.id,
            amount=TicketAmount_Class_B,
            ledger=LedgerTickets,
            code=1,
        ),

    ])
    if transfer_errors:
        print(transfer_errors)
    assert len(transfer_errors) == 0
    print('✅ initial transfers executed')
    return


# ----------------------------
# TigerBeetle placeholders (replace with real TB RPCs)
# ----------------------------
# These are stubs; wire to your TB client.
def hold_tickets(ticket_class: str, qty: int, reservation_id: str) -> None:
    # TODO: call TB; raise on insufficient inventory
    return None


def commit_order(order: Order) -> None:
    # TODO: TB: commit hold, settle money flows using order.id as transfer id
    return None


def rollback_reservation(reservation_id: str) -> None:
    # TODO: TB: move hold back to inventory idempotently
    return None


def try_grant_goodie(db: Session, ticket_class: str, user_id: str, order_id: str) -> bool:
    # In real life: TB atomic transfer goodie_pool:class -> goodie_user:user_id (idempotent via order_id)
    row = db.get(GoodiesCounter, ticket_class)
    if row.granted >= TicketAmount_first_n:
        return False
    row.granted += 1
    db.commit()
    return True


if __name__ == '__main__':
    with tb.ClientSync(cluster_id=0, replica_addresses=os.getenv("TB_ADDRESS", "3000")) as client:

        create_accounts(client)
        initial_transfers(client)
