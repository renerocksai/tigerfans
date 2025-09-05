# ⭐ TigerFans

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

### Start Tigerbeetle

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

