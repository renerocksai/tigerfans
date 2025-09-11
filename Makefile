.phony = clean, server-w2, server-w3, tb, psql

clean:
	rm -f ./demo.db*
	rm -f ./data/0_0.tigerbeetle


tb:
	./start_tigerbeetle.sh

server:
	uvicorn tigerfans.server:app --reload --workers=1

server-w2:
	 DATABASE_URL="postgresql+asyncpg://postgres:devpass@127.0.0.1:5432/tigerfans" uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=2

server-w3:
	 DATABASE_URL="postgresql+asyncpg://devuser:devpass@127.0.0.1:5432/tigerfans" uvicorn tigerfans.server:app --host 0.0.0.0 --port 8000 --workers=3

psql:
	sudo docker run -d --name pg \
      -e POSTGRES_USER=devuser \
	  -e POSTGRES_PASSWORD=devpass \
	  -e POSTGRES_DB=tigerfans \
	  -p 5432:5432 \
	  -v pgdata:/var/lib/postgresql/data \
	  --shm-size=512m \
	  postgres:16
