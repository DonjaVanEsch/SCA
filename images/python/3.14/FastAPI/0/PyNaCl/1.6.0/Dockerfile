FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libffi-dev \
    libsodium-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip wheel cffi pycparser \
    && (python -c "import setuptools" 2>/dev/null || pip install --no-cache-dir setuptools) \
    && pip install --no-cache-dir --no-build-isolation -r requirements.txt

COPY app.py .

EXPOSE 8000

CMD ["python", "app.py"]
