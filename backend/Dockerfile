# Axiom backend Docker image (Python 3.12 slim)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/backend

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The worker service runs as uid/gid 65532 (see docker-compose.yml).
# The id has to exist in the image's user and group database, otherwise
# the worker cannot resolve its own group: Celery then assumes it is
# running as root and prints a "superuser privileges" warning that is
# both alarming and false.
RUN groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
