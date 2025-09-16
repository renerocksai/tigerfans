.phony = clean, server, server-w1, server-w2, server-w3, tb, psql, caddy

POSTGRES_USER    := devuser
POSTGRES_PASSWORD := devpass
POSTGRES_DB      := tigerfans

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
