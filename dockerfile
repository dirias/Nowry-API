FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    libopenblas-dev \
    libomp-dev \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ./requirements.txt .


RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
