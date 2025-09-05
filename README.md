# ⭐ TigerFans

## Run the demo in docker

_(I tested this on an M3 Mac)_

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

To see the last 200 orders, go to
[http://localhost:8000/admin](http://localhost:8000/admin) and log in with
username `admin` and password `supasecret`.
