FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /data
ENV DATABASE_URL=sqlite+aiosqlite:////data/playerok.db
VOLUME ["/data"]

CMD ["python", "bot.py"]
