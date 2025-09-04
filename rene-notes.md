## Ledgers and Accounts

We use 4-digit accounts, where the thoushands value indicates some sort of
group. Even broader will be ledgers. Since since we have to use double-entry
accounting, we need "counter" accounts for counters and stats. These counter
accounts end in digit 5, whereas the real data's accounts end in digit 0.
ledger 1000: app stats (counters)

- restart-counter, restart-counter counter-account


### ledger 1000 : Stats

account 1000 : restart-counter_spent
account 1005 : restart-counter_budget


### ledger 2000 : tickets

         +-------- ticket type: 1.. first class, 2.. 2nd class
         v
account 2110 : class_A_first_100_spent  = 100 debit
account 2115 : class_A_first_100_budget = 100 credit

With each sold ticket, we try to debit from the budget and credit it to the spent.

account 2120 : class_A_tickets-spent
account 2125 : class_A_tickets-budget

account 2210 : class_B_first_100_spent  = 100 debit
account 2215 : class_B_first_100_budget = 100 credit

account 2120 : class_B_tickets-spent
account 2125 : class_B_tickets-budget
