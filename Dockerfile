FROM python:3.12-slim

WORKDIR /srv

# psycopg2-binary needs no build deps, but keep the image lean anyway.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway assigns $PORT at runtime. Shell form so the variable expands;
# default to 8000 for local `docker run`.
CMD uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}
