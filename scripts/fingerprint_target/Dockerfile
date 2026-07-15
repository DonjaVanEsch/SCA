FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /certs \
    && openssl req -x509 -newkey rsa:2048 -keyout /certs/key.pem -out /certs/cert.pem \
       -days 3650 -nodes -subj "/CN=pqc-fingerprint-target"

WORKDIR /app
COPY app.py .

EXPOSE 9000 9443

CMD ["python", "app.py"]
