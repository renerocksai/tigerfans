# model/accounting/__init__.py
import os
from ._tigerbeetle import (
    TicketAmount_Class_A, TicketAmount_Class_B, TicketAmount_first_n
)

BACKEND = os.getenv("ACCT_BACKEND", "tb").lower()  # 'tb' | 'pg'

if BACKEND == "pg":
    from ._postgres import (
        create_accounts, initial_transfers, hold_tickets, commit_order,
        cancel_order, cancel_only_goodie, compute_inventory, count_goodies
    )
else:
    from ._tigerbeetle import (
        create_accounts, initial_transfers, hold_tickets, commit_order,
        cancel_order, cancel_only_goodie, compute_inventory, count_goodies,
    )


__all__ = [
  "create_accounts", "initial_transfers", "hold_tickets", "commit_order",
  "cancel_order", "cancel_only_goodie", "compute_inventory", "count_goodies",
  "BACKEND",
  "TicketAmount_Class_A", "TicketAmount_Class_B", "TicketAmount_first_n",
]
