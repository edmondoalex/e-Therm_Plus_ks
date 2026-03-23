FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY app /app
COPY config.yaml /app/config.yaml
RUN pip install --no-cache-dir paho-mqtt
CMD ["python", "main.py"]
