import os
import tigerbeetle as tb
from typing import Tuple, List
from ...helpers import now_ts, to_iso
import gc
import asyncio

# Config
TicketAmount_Class_A = 5_000_000
TicketAmount_Class_B = 5_000_000
TicketAmount_first_n = 100_000

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


def debug_event_loop():
    print(f"🔍 Event loop: {asyncio.get_event_loop()}")
    print(f"🔍 Current task: {asyncio.current_task()}")
    print(f"🔍 Pending tasks: {len(asyncio.all_tasks())}")

    # Check for zombie futures
    futures = []
    for obj in gc.get_objects():
        if isinstance(obj, asyncio.Future):
            futures.append(obj)
    print(f"🔍 Live futures: {len(futures)}")

    # Check for cancelled tasks
    cancelled = 0
    for task in asyncio.all_tasks():
        if task.cancelled():
            cancelled += 1
    print(f"🔍 Cancelled tasks: {cancelled}")


class TransferBatcher:
    def __init__(self, client: tb.ClientAsync, max_batch_size: int = 8190,
                 flush_timeout: float = 0.1):
        self.client = client
        self.max_batch_size = max_batch_size
        self.flush_timeout = flush_timeout
        self._lock = asyncio.Lock()
        self._pending_transfers: List[tb.Transfer] = []
        self._pending_completions: List[Tuple[int, int, asyncio.Future]] = []
        self._flush_task: asyncio.Task | None = None

    async def submit(
        self, transfers: List[tb.Transfer]
    ) -> List[tb.CreateTransferResult]:
        # print("submit", [f"id={t.id}" for t in transfers])
        if not transfers:
            return []

        async with self._lock:
            start_index = len(self._pending_transfers)
            self._pending_transfers.extend(transfers)
            fut = asyncio.Future()
            self._pending_completions.append(
                (start_index, len(transfers), fut)
            )

            should_flush_now = (
                len(self._pending_transfers) >= self.max_batch_size
            )
            should_schedule_delayed = (
                self._flush_task is None or self._flush_task.done()
            )

        if should_flush_now:
            # print("submit: flushing immediately")
            await self._flush()
        elif should_schedule_delayed:
            # print("submit: scheduling delayed flush")
            self._flush_task = asyncio.create_task(self._delayed_flush())

        return await fut

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self.flush_timeout)
            await self._flush()
        except asyncio.CancelledError:
            # Clean cancellation - just exit
            pass
        except Exception as e:
            print(f"_delayed_flush error: {e}")

    async def _flush(self) -> None:
        # Extract under lock
        batch = None
        completions = []
        async with self._lock:
            if not self._pending_transfers:
                return
            batch = self._pending_transfers[:]
            completions = self._pending_completions[:]
            self._pending_transfers = []
            self._pending_completions = []

            # Safe cancellation: only cancel if it's a different task
            if (
                self._flush_task and not self._flush_task.done() and
                self._flush_task is not asyncio.current_task()
            ):
                # print("Safely cancelling previous flush task")
                self._flush_task.cancel()
                # Don't await it - let it clean up on its own
            self._flush_task = None

        # debug_event_loop()
        error_results = await self.client.create_transfers(batch)

        # Reconstruct full results: None for success, error for failures
        full_results = [None] * len(batch)
        for error_result in error_results:
            full_results[error_result.index] = error_result

        # Resolve futures
        for start, num, fut in completions:
            sub_slice = full_results[start:start + num]
            sub_results = [r for r in sub_slice if r is not None]
            fut.set_result(sub_results)


async def create_accounts(client: tb.ClientAsync):
    account_errors = await client.create_accounts([
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


async def initial_transfers(client: tb.ClientAsync):
    transfer_errors = await client.create_transfers([
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


async def hold_tickets(
    batcher: TransferBatcher, ticket_class: str,
    qty: int, timeout_seconds: int,
) -> Tuple[str, str, bool, bool]:
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

    transfers = [
        tb.Transfer(
            id=tb_transfer_id,
            debit_account_id=debit_account_id,
            credit_account_id=credit_account_id,
            amount=qty,
            ledger=LedgerTickets,
            code=20,
            timeout=timeout_seconds,
            flags=tb.TransferFlags.PENDING,
        ),
        tb.Transfer(
            id=goodie_tb_transfer_id,
            debit_account_id=First_n_budget.id,
            credit_account_id=First_n_spent.id,
            amount=1,
            ledger=LedgerTickets,
            code=20,
            timeout=timeout_seconds,
            flags=tb.TransferFlags.PENDING,
        ),
    ]

    print("hold_tickets calling batcher")
    transfer_errors = await batcher.submit(transfers)
    print("batcher returned")

    has_ticket = True
    has_goodie = True
    for transfer_error in transfer_errors:
        if transfer_error.index == 0:
            has_ticket = False
        if transfer_error.index == 1:
            has_goodie = False
    return tb_transfer_id, goodie_tb_transfer_id, has_ticket, has_goodie


async def book_immediately(
    batcher: TransferBatcher, ticket_class: str,
    qty: int,
) -> Tuple[str, str, bool, bool]:
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

    transfers = [
        tb.Transfer(
            id=tb_transfer_id,
            debit_account_id=debit_account_id,
            credit_account_id=credit_account_id,
            amount=qty,
            ledger=LedgerTickets,
            code=20,
        ),
        tb.Transfer(
            id=goodie_tb_transfer_id,
            debit_account_id=First_n_budget.id,
            credit_account_id=First_n_spent.id,
            amount=1,
            ledger=LedgerTickets,
            code=20,
        ),
    ]
    transfer_errors = await batcher.submit(transfers)
    has_ticket = True
    has_goodie = True
    for transfer_error in transfer_errors:
        if transfer_error.index == 0:
            has_ticket = False
        if transfer_error.index == 1:
            has_goodie = False
    return tb_transfer_id, goodie_tb_transfer_id, has_ticket, has_goodie


async def commit_order(
    batcher: TransferBatcher,
    tb_transfer_id: str | int, goodie_tb_transfer_id: str | int,
    ticket_class: str, qty: int, try_goodie: bool,
) -> Tuple[bool, bool]:
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

    transfers = [
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
    ]

    if try_goodie:
        transfers.append(
            tb.Transfer(
                id=id_post_goodies,
                debit_account_id=First_n_budget.id,
                credit_account_id=First_n_spent.id,
                amount=1,
                pending_id=goodie_tb_transfer_id,
                ledger=LedgerTickets,
                code=20,
                flags=tb.TransferFlags.POST_PENDING_TRANSFER,
            )
        )
    transfer_errors = await batcher.submit(transfers)

    has_ticket = True
    has_goodie = try_goodie
    for transfer_error in transfer_errors:
        if transfer_error.index == 0:
            has_ticket = False
        if transfer_error.index == 1:
            has_goodie = False

    return has_ticket, has_goodie


async def cancel_only_goodie(
    batcher: TransferBatcher,
    goodie_tb_transfer_id: str | int
) -> None:
    if isinstance(goodie_tb_transfer_id, str):
        goodie_tb_transfer_id = int(goodie_tb_transfer_id)
    id_void_goodies = tb.id()
    transfers = [
        tb.Transfer(
            id=id_void_goodies,
            debit_account_id=First_n_budget.id,
            credit_account_id=First_n_spent.id,
            amount=1,
            pending_id=goodie_tb_transfer_id,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.VOID_PENDING_TRANSFER,
        ),
    ]
    transfer_errors = await batcher.submit(transfers)
    if transfer_errors:
        # we don't really care
        pass
    return None


async def cancel_order(
    batcher: TransferBatcher,
    tb_transfer_id: str | int, goodie_tb_transfer_id: str | int,
    ticket_class: str, qty: int,
) -> None:
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

    id_void = tb.id()
    id_void_goodies = tb.id()

    transfers = [
        tb.Transfer(
            id=id_void,
            debit_account_id=debit_account_id,
            credit_account_id=credit_account_id,
            amount=qty,
            pending_id=tb_transfer_id,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.VOID_PENDING_TRANSFER,
        ),
        tb.Transfer(
            id=id_void_goodies,
            debit_account_id=First_n_budget.id,
            credit_account_id=First_n_spent.id,
            amount=1,
            pending_id=goodie_tb_transfer_id,
            ledger=LedgerTickets,
            code=20,
            flags=tb.TransferFlags.VOID_PENDING_TRANSFER,
        ),
    ]

    transfer_errors = await batcher.submit(transfers)

    if transfer_errors:
        # we don't really care
        pass

    return None


async def compute_inventory(client: tb.ClientAsync) -> dict:
    accounts = await client.lookup_accounts(
        [Class_A_spent.id, Class_B_spent.id]
    )
    out = {}
    now = now_ts()
    for ticket_class, account in zip(['A', 'B'], accounts):
        sold = account.credits_posted
        held = account.credits_pending
        budget = TicketAmount_Class_A
        if ticket_class == 'B':
            budget = TicketAmount_Class_B
        available = budget - sold - held
        out[ticket_class] = {
            "capacity": budget,
            "sold": sold,
            "active_holds": held,
            "available": available,
            "sold_out": available <= 0,
            "timestamp": to_iso(now),
        }
    return out


async def count_goodies(client: tb.ClientAsync) -> int:
    accounts = await client.lookup_accounts([First_n_spent.id])
    return accounts[0].credits_posted


if __name__ == '__main__':
    with tb.ClientSync(
        cluster_id=0,
        replica_addresses=os.getenv("TB_ADDRESS", "3000")
    ) as client:
        create_accounts(client)
        initial_transfers(client)
