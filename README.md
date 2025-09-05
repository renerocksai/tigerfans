# ⭐ TigerFans

## First-time setup

* download tigerbeetle into this directory

### Create environment for python

```console
$ python3 -m venv venv
$ source venv/bin/activate
$ pip install -r requirements.txt
```

### Init Tigerbeetle

```console

# init and start tigerbeetle
$ ./init_tigerbeetle.sh
$ python init_accounts.py
```

### Start the server

With Tigerbeetle running:

```console
$ uvicorn tigerfans.server:app --reload --port=8888
```

