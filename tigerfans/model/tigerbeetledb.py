import os
import tigerbeetle as tb
from typing import Tuple
from ..helpers import now_ts, to_iso

# Config
TicketAmount_Class_A = 100
TicketAmount_Class_B = 500
TicketAmount_first_n = 100

RestartCounter_max = 1_000_000

LedgerTickets = 2000

# Resources:
# init: debit Operator, credit budget
# allocate: debit budget, credit spent

First_n_Operator = tb.Account(id=2110, ledger=LedgerTickets, code=20)
First_n_budget = tb.Account(
        id=2115, ledger=LedgerTickets, code=20,
        flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS
    )
First_n_spent = tb.Account(id=2119, ledger=LedgerTickets, code=20)

Class_A_Operator = tb.Account(id=2120, ledger=LedgerTickets, code=20)
Class_A_budget = tb.Account(
        id=2125, ledger=LedgerTickets, code=20,
        flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS
    )
Class_A_spent = tb.Account(id=2129, ledger=LedgerTickets, code=20)


Class_B_first_n_Operator = tb.Account(id=2210, ledger=LedgerTickets, code=20)
Class_B_first_n_budget = tb.Account(
        id=2215, ledger=LedgerTickets, code=20,
        flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS
    )
Class_B_first_n_spent = tb.Account(id=2219, ledger=LedgerTickets, code=20)

Class_B_Operator = tb.Account(id=2220, ledger=LedgerTickets, code=20)
Class_B_budget = tb.Account(
        id=2225, ledger=LedgerTickets, code=20,
        flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS
    )
Class_B_spent = tb.Account(id=2229, ledger=LedgerTickets, code=20)


def create_accounts(client: tb.ClientSync):
    account_errors = client.create_accounts([
        First_n_Operator,
        First_n_spent,
        First_n_budget,
        Class_A_Operator,
        Class_A_spent,
        Class_A_budget,
        Class_B_first_n_Operator,
        Class_B_first_n_spent,
        Class_B_first_n_budget,
        Class_B_Operator,
        Class_B_spent,
        Class_B_budget,
    ])

    if account_errors:
        print(account_errors)
        return False
    else:
        print('✅ accounts created')
    return True


def initial_transfers(client: tb.ClientSync):
    transfer_errors = client.create_transfers([
        tb.Transfer(
            id=tb.id(),
            debit_account_id=First_n_Operator.id,
            credit_account_id=First_n_budget.id,
            amount=TicketAmount_first_n,
            ledger=LedgerTickets,
            code=1,
        ),
        tb.Transfer(
            id=tb.id(),
            debit_account_id=Class_B_first_n_Operator.id,
            credit_account_id=Class_B_first_n_budget.id,
            amount=TicketAmount_first_n,
            ledger=LedgerTickets,
            code=1,
        ),

        tb.Transfer(
            id=tb.id(),
            debit_account_id=Class_A_Operator.id,
            credit_account_id=Class_A_budget.id,
            amount=TicketAmount_Class_A,
            ledger=LedgerTickets,
            code=1,
        ),
        tb.Transfer(
            id=tb.id(),
            debit_account_id=Class_B_Operator.id,
            credit_account_id=Class_B_budget.id,
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


def hold_tickets(client: tb.ClientSync, ticket_class: str, qty: int, timeout_seconds=int) -> Tuple[str, str]:
    # we issue 2 transfers: one for the actual tickets and one for the goodie
    # counter
    if ticket_class not in ['A', 'B']:
        raise ValueError("Unknown class " + ticket_class)

    if ticket_class == 'A':
        debit_account_id = Class_A_budget.id
        credit_account_id = Class_A_spent.id
    else:
        debit_account_id = Class_B_budget.id
        credit_account_id = Class_B_spent.id

    tb_transfer_id = tb.id()
    goodie_tb_transfer_id = tb.id()

    transfer_errors = client.create_transfers([
        tb.Transfer(
            id=tb_transfer_id,
            debit_account_id=debit_account_id,
            credit_account_id=credit_account_id,
            amount=qty,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.PENDING,
        ),
        tb.Transfer(
            id=goodie_tb_transfer_id,
            debit_account_id=First_n_budget.id,
            credit_account_id=First_n_spent.id,
            amount=1,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.PENDING,
        ),
    ])
    if transfer_errors:
        raise RuntimeError(transfer_errors)
    return tb_transfer_id, goodie_tb_transfer_id


def commit_order(client: tb.ClientSync, tb_transfer_id: str | int, goodie_tb_transfer_id: str | int, ticket_class: str, qty: int) -> Tuple[bool, bool]:
    if ticket_class not in ['A', 'B']:
        raise ValueError("Unknown class " + ticket_class)

    if isinstance(tb_transfer_id, str):
        tb_transfer_id = int(tb_transfer_id)
    if isinstance(goodie_tb_transfer_id, str):
        goodie_tb_transfer_id = int(goodie_tb_transfer_id)

    if ticket_class == 'A':
        debit_account_id = Class_A_budget.id
        credit_account_id = Class_A_spent.id
    else:
        debit_account_id = Class_B_budget.id
        credit_account_id = Class_B_spent.id

    id_post = tb.id()
    id_post_goodies = tb.id()

    transfer_errors = client.create_transfers([
        tb.Transfer(
            id=id_post,
            debit_account_id=debit_account_id,
            credit_account_id=credit_account_id,
            amount=qty,
            pending_id=tb_transfer_id,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.POST_PENDING_TRANSFER,
        ),
        tb.Transfer(
            id=id_post_goodies,
            debit_account_id=First_n_budget.id,
            credit_account_id=First_n_spent.id,
            amount=1,
            pending_id=goodie_tb_transfer_id,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.POST_PENDING_TRANSFER,
        ),
    ])

    has_ticket = True
    has_goodie = True
    print(transfer_errors)
    for transfer_error in transfer_errors:
        if transfer_error.index == 0:
            has_ticket = False
        if transfer_error.index == 1:
            has_goodie = False

    return has_ticket, has_goodie


def cancel_order(client: tb.ClientSync, tb_transfer_id: str | int, goodie_tb_transfer_id: str | int, ticket_class: str, qty: int) -> None:
    if ticket_class not in ['A', 'B']:
        raise ValueError("Unknown class " + ticket_class)

    if isinstance(tb_transfer_id, str):
        tb_transfer_id = int(tb_transfer_id)
    if isinstance(goodie_tb_transfer_id, str):
        goodie_tb_transfer_id = int(goodie_tb_transfer_id)

    if ticket_class == 'A':
        debit_account_id = Class_A_budget.id
        credit_account_id = Class_A_spent.id
    else:
        debit_account_id = Class_B_budget.id
        credit_account_id = Class_B_spent.id

    id_post = tb.id()
    id_post_goodies = tb.id()

    transfer_errors = client.create_transfers([
        tb.Transfer(
            id=id_post,
            debit_account_id=debit_account_id,
            credit_account_id=credit_account_id,
            amount=qty,
            pending_id=tb_transfer_id,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.VOID_PENDING_TRANSFER,
        ),
        tb.Transfer(
            id=id_post_goodies,
            debit_account_id=First_n_budget.id,
            credit_account_id=First_n_spent.id,
            amount=1,
            pending_id=goodie_tb_transfer_id,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.VOID_PENDING_TRANSFER,
        ),
    ])

    if transfer_errors:
        pass

    return None


def compute_inventory(client: tb.ClientSync):
    accounts = client.lookup_accounts([Class_A_spent.id, Class_B_spent.id])
    out = {}
    now = now_ts()
    for ticket_class, account in zip(['A', 'B'], accounts):
        sold = account.credits_posted
        held = account.credits_pending
        budget = TicketAmount_Class_A if ticket_class == 'A' else TicketAmount_Class_B
        available = budget - sold - held
        out[ticket_class] = {
            "capacity": TicketAmount_Class_A,
            "sold": sold,
            "active_holds": held,
            "available": available,
            "sold_out": available <= 0,
            "timestamp": to_iso(now),
        }
    return out


if __name__ == '__main__':
    with tb.ClientSync(
        cluster_id=0,
        replica_addresses=os.getenv("TB_ADDRESS", "3000")
    ) as client:
        create_accounts(client)
        initial_transfers(client)
