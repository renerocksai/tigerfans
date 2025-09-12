# ⭐ TigerFans

_Resource reservations, payment flows, and consistency — in one clean demo._

See a live-demo [here](https://tigerfans.technologylab.ai)!

TigerFans is a prototype **ticketing system demo** that shows how
**[TigerBeetle](https://tigerbeetle.com)** can be applied beyond financial
transactions. It models a fictional **conference booking system** with a payment
flow:

![](doc/demo.gif)

- **Checkout** creates an order and places a **time-limited hold**
- An **external payment step** decides the outcome
- A **webhook callback** from the payment provider then **finalizes** or
  **voids** the order

The system demonstrates **time-limited holds** (pending transfers) for tickets
and a **conditional goodie grant**: the ticket is committed on payment success,
and a goodie is granted if goodies are still available.

> 💡 This demo is not about benchmarking or raw performance. It’s meant to show
how TigerBeetle fits into a **realistic booking flow**, rather than just an
isolated code snippet. It also highlights how tickets and goodies are modeled as
**TigerBeetle accounts and transfers**, which works very differently from rows
in an SQL table.

### Features

- **Two ticket classes** (A: Premium, B: Standard), **one ticket per order**.
- **Time-limited holds** via **pending TigerBeetle transfers**; finalized on
  payment success or **voided on timeout/failure**.
- 🎁 **Goodie unlocks**: first 100 paid orders, granted with the ticket.
- **MockPay** provider with **redirect + webhook** flow (no real payments).
- **FastAPI** backend. Python is `easy`.
- **SQLite** or **PostgreSQL** for app state (orders, payments).
- **Admin dashboard** (basic, protected by HTTP Basic Auth)
-  **UI pages**: landing, checkout, success incl. QR-code download

> 📄 **See [doc/tigerbeetle.md](doc/tigerbeetle.md) for more details about how we
model TigerBeetle accounts and transfers.**

## First-time setup

* download and unzip [Tigerbeetle](https://tigerbeetle.com) into this directory.
See [here](https://tigerbeetle.com/#install) how to.

### Create environment for python

```console
$ python3 -m venv venv
$ source venv/bin/activate
$ pip install -r requirements.txt
```

## Start TigerFans:

### Start TigerBeetle

```console

# init and start tigerbeetle
$ ./start_tigerbeetle.sh
```

### Start the server

With Tigerbeetle running:

```console
$ uvicorn tigerfans.server:app --reload --port=8000
```
Connect your browser to [http://localhost:8000/](http://localhost:8000).

To see the last 200 orders, go to
[http://localhost:8000/admin](http://localhost:8000/admin) and log in with
username `admin` and password `supasecret`.

**Note:** Above commands start TigerFans in development mode where sqlite is
used as a database. Because SQLite is an in-process database, only 1 worker is
allowed. Don't expect Oasis scale 65 tickets per second = O(1) performance with
this setup.

---

## TigerFans in Production

To run TigerFans in a more production-like environment, we recommend:

- A host with >= 4 vCPUs, >= 4 GB of RAM
- PostgreSQL installed natively. See our [postgresql.conf](./postgresql.conf).
  - For a quick start, you can run PostgreSQL in docker. See
    [Makefile](./Makefile) target `psql` for an example.
- Start uvicorn with 3 workers on a 4-CPU machine. (Makefile target
  `server-w3`).
- Tigerbeetle in development mode (single node) on the same node seems fine.
  (Makefile target `tb`).
- Use [Caddy](https://caddyserver.com) as reverse proxy for HTTPS support. See
  the `caddy` target in the [Makefile](./Makefile) and
  [docker-compose-caddy](./docker-compose-caddy.yml).

On a `c7g.xlarge` EC2 instance, we achieve O(1.7) = 110 tickets per second with
above setup and a top score of O(1.99) = 129.3 tickets per second:

```console
$ cd tigerfans
$ python load_client.py --base http://localhost:8000 --total 1000 --concurrency 30 --fail-rate 0 --poll-interval 0.1

=== Load Summary ===
Total: 1000   OK: 1000   PAID: 1000   FAILED: 0   CANCELED: 0   TIMEOUT: 0   ERROR: 0
Latency (observed order resolution): avg 0.041s   p50 0.042s   p90 0.083s   p99 0.164s
Wall time: 7.736s   Throughput: 129.3 ops/s
```

---

## Run the demo in docker

> ⚠️ Works on macOS (M3 tested), but not on AWS due to io_uring limitations in
> Docker.

```console
$ docker-compose up --build
```
Wait for the app to start:

```
app-1  | INFO:     Application startup complete.
```

Then connect your browser to [http://localhost:8000/](http://localhost:8000).

To see the last 200 orders, go to
[http://localhost:8000/admin](http://localhost:8000/admin) and log in with
username `admin` and password `supasecret`.
