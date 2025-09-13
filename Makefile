.phony = clean, server-w2, server-w3, tb, psql, caddy

clean:
	rm -f ./demo.db*
	rm -f ./data/0_0.tigerbeetle


tb:
	./start_tigerbeetle.sh

# 1 worker, sqlite, auto-reload on code changes, for testing locally
server:
	uvicorn tigerfans.server:app --reload --workers=1

# 2 workers, postgres
# needs postgres installed. use psql target below if necesary.
# postgres user is set to `postgres` below to accomodate for psql in docker.
server-w2:
	 DATABASE_URL="postgresql+asyncpg://postgres:devpass@127.0.0.1:5432/tigerfans" uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=2

# 3 workers, postgres, no access log to stdout
# needs postgres installed.
# postgres user is set to `devuser` below, so configure your postgres
# accordingly.
server-w3:
	 DATABASE_URL="postgresql+asyncpg://devuser:devpass@127.0.0.1:5432/tigerfans" uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=3 --no-access-log

# we start with 2 workers as we expect async redis to be efficient
server-redis:
	 DATABASE_URL="redis://127.0.0.1:6379/0" uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=2 --no-access-log

# this is only for the mac. on prod, we use native postgres w/o docker
#
# note: the -e POSTGRES_USER setting does not seem to work. user remains set to
#       `postgres`.
psql:
	docker run -d --name pg \
      -e POSTGRES_USER=devuser \
	  -e POSTGRES_PASSWORD=devpass \
	  -e POSTGRES_DB=tigerfans \
	  -p 5432:5432 \
	  -v pgdata:/var/lib/postgresql/data \
	  --shm-size=512m \
	  postgres:16

redis:
	${SUDO} docker run -d --name redis \
	  -p 6379:6379 \
	  -v redisdata:/data \
	  --ulimit nofile=100000:100000 \
	  --sysctl net.core.somaxconn=1024 \
	  redis:7 \
	  redis-server \
	    --appendonly yes \
	    --appendfsync everysec \
	    --save "" \
	    --tcp-keepalive 300 \
	    --io-threads 4 \
	    --io-threads-do-reads yes

# use caddy as reverse proxy for https
caddy:
	sudo docker-compose -f docker-compose-caddy.yml up
