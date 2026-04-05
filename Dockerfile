FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    mcp \
    httpx \
    anthropic \
    python-dotenv \
    fastapi \
    uvicorn \
    web3 \
    eth-account \
    cryptography \
    x402

COPY . .

ENV PHOENIXD_URL=http://host.docker.internal:9740
ENV PHOENIXD_PASSWORD=""
ENV ANTHROPIC_API_KEY=""

EXPOSE 8002

CMD ["python3", "server.py"]
