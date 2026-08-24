FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway ./gateway

EXPOSE 8001 14550/udp
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8001"]
