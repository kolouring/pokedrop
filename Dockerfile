FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/pokedrop.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pokedrop ./pokedrop

RUN useradd --create-home --uid 1000 pokedrop \
    && mkdir -p /data \
    && chown -R pokedrop:pokedrop /data /app
USER pokedrop

VOLUME ["/data"]

CMD ["python", "-m", "pokedrop"]
