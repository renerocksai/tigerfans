.phony = clean, server, server-w1, server-w2, server-w3, tb, psql, caddy, load-tests-server-w1, load-tests-smoketest, load-tests-print, load-tests-debug

POSTGRES_USER    := devuser
POSTGRES_PASSWORD := devpass
POSTGRES_DB      := tigerfans

# default load_test config
LT_ENV_DIR   ?= ./load_tests
LT_ENV_GLOB  ?= *.env
LT_CSV       ?= load_tests.csv
LT_LOG       ?= load_tests.log
LT_SERVER_CMD?= make server-w1
LT_REPEAT    ?= 3

clean:
	rm -f ./demo.db*
	rm -f ./data/0_0.tigerbeetle


tb:
	./start_tigerbeetle.sh

# 1 worker, postgres, auto-reload on code changes, for testing locally
server:
	REDIS_URL="redis://127.0.0.1:6379/0" \
	DATABASE_URL="postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:5432/$(POSTGRES_DB)" \
	uvicorn tigerfans.server:app --reload --workers=1

# 1 worker, postgres
# needs postgres installed. use psql target below if necesary.
# redis on Linux via unix socket
# postgres user is set to `postgres` below to accomodate for psql in docker.
server-w1:
	REDIS_URL="unix:///$(CURDIR)/data/redis.sock?db=0" \
	DATABASE_URL="postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:5432/$(POSTGRES_DB)" \
	uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=1 --no-access-log

# 2 workers, postgres
# needs postgres installed. use psql target below if necesary.
# redis on Linux via unix socket
# postgres user is set to `postgres` below to accomodate for psql in docker.
server-w2:
	REDIS_URL="unix:///$(CURDIR)/data/redis.sock?db=0" \
	DATABASE_URL="postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:5432/$(POSTGRES_DB)" \
	uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=2 --no-access-log

# 3 workers, postgres, no access log to stdout
# needs postgres installed.
# redis on Linux via unix socket
server-w3:
	REDIS_URL="unix:///$(CURDIR)/data/redis.sock?db=0" \
	DATABASE_URL="postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:5432/$(POSTGRES_DB)" \
	uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=3 --no-access-log

# this is only for the mac. on prod, we use native postgres w/o docker
psql:
	docker run -d --name pg \
	-e POSTGRES_USER=$(POSTGRES_USER) \
	-e POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
	-e POSTGRES_DB=$(POSTGRES_DB) \
	-p 5432:5432 \
	-v pgdata:/var/lib/postgresql/data \
	--shm-size=512m \
	postgres:16

# redis with TCP and UNIX socket
redis:
	${SUDO} docker run -d --name redis \
	-p 6379:6379 \
	-v $(CURDIR)/data:/data \
	--ulimit nofile=100000:100000 \
	--sysctl net.core.somaxconn=1024 \
	redis:7 \
	redis-server \
	--appendonly no \
	--save "" \
	--dbfilename "" \
	--tcp-keepalive 300 \
	--io-threads 4 \
	--io-threads-do-reads yes \
	--unixsocket /data/redis.sock \
	--unixsocketperm 777

# use caddy as reverse proxy for https
caddy:
	sudo docker-compose -f docker-compose-caddy.yml up


# we don't use tee as it would die first and cause broken stdout/sterr pipes
# on ctrl+c
load-tests:
	@echo "==> Running load tests, output is being logged to $(LT_LOG)"
	@echo "==> Use: tail -f $(LT_LOG)"
	@find $(LT_ENV_DIR) -name '$(LT_ENV_GLOB)' | \
	python benchctl.py --env-file=- --csv-file=$(LT_CSV) \
	  --server-cmd='$(LT_SERVER_CMD)' --repeat=$(LT_REPEAT) \
	>> $(LT_LOG) 2>&1

load-tests-print:
	@find $(LT_ENV_DIR) -name '*.env' -print | sort

load-tests-mock:
	@echo "==> Running mock load tests, output is being logged to $(LT_LOG)"
	@echo "==> Use: tail -f $(LT_LOG)"
	@find $(LT_ENV_DIR) -name '$(LT_ENV_GLOB)' | \
	python benchctl.py --env-file=- --csv-file=$(LT_CSV) \
	  --server-cmd='sleep 60' --repeat=$(LT_REPEAT) \
	>> $(LT_LOG) 2>&1
