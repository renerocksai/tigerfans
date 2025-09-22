# How we use TigerBeetle

## Accounts

We manage three resources:

- class A tickets
- class B tickets
- goodies for the first n buyers

We model resource counting and booking with three TigerBeetle accounts per
resource:

- resource_Operator account
- resource_budget account
- resource_spent account

When initializing, we fund the budget accounts via their operator accounts.

```
INIT FLOW: Fund the budget
==========================

+----------+            +----------+            +----------+
|          | --credit-->|          |            |          |
| Operator |            |  Budget  |            |  Spent   |
|          | <--debit-- |          |            |          |
+----------+            +----------+            +----------+
```

After that, for each booking, we debit the budget account and credit the spent
account. If this fails, we're sold out.


```
BOOKING FLOW: Spend the budget
==============================

+----------+            +----------+            +----------+
|          |            |          | --credit-->|          |
| Operator |            |  Budget  |            |  Spent   |
|          |            |          | <--debit-- |          |
+----------+            +----------+            +----------+
```

## Limited holds

On checkout, when the user has selected which ticket to buy and is being
redirected to the payment, provider, we place a hold on the tickets and goodies
resource. We perform pending transfers with a timeout of a few minutes.

```
HOLDING FLOW: Limited Hold on the resource
==========================================

Tickets:
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |     flags=PENDING, timeout=5min     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+


Goodies:
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |     flags=PENDING, timeout=5min     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+
```

Now, each of those can fail. If the goodies transfer fails, we don't worry. That
means there aren't any goodies left. We record that in the order and won't
attempt to finalize the pending goodie transfer when we receive payment.

However, if the tickets transfer fails, it means we're sold out. There might be
some pending transfers from other customers whose payments might fail soon or
who might abandon the payment page, so there is still a small chance that
checking back later will resolve the "sold out" situation. So while there's
still a chance, we display that information to the customer and encourage them
to try again.

## Successful payments

```
COMMIT FLOW: Post the pending transfers
=======================================

Tickets:
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |     flags=POST_PENDING_TRANSFER     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+


Goodies: if applicable
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |     flags=POST_PENDING_TRANSFER     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+
```

If a successful payment is received, we post the pending transfers. If it goes
through OK, we show the customer their ticket and potential bonus (goodie).

What can go wrong here is: the timeouts might have expired. In that case, we
don't give up, we issue immediate transfers:

```
ON TIMEOUT FLOW: Post transfers immediately ("re-try")
======================================================

Tickets:
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |                                     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+


Goodies: if applicable
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |                                     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+
```

If the tickets transfer fails, we notify the user via status 'PAID_UNFULFILLED'
and a short message that they will be refunded.

## FAILED / canceled payments

... are easy: we just void the pending transfers:

```
CANCEL FLOW: Void the pending transfers
=======================================

Tickets:
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |     flags=VOID_PENDING_TRANSFER     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+


Goodies: if applicable
      +----------+     ----------credit---------->     +----------+
      |          |                                     |          |
      |  Budget  |     flags=VOID_PENDING_TRANSFER     |  Spent   |
      |          |                                     |          |
      +----------+     <---------debit------------     +----------+
```

## Check the source code

If you want to see how we implemented all of the above, please check the source
code.

- [_tigerbeetledb.py](../tigerfans/model/accounting/_tigerbeetle.py) has all the
  TigerBeetle accounts and transfers.
- Check out how [server.py](../tigerfans/server.py) uses them to model the
  flows we illustrated above.

