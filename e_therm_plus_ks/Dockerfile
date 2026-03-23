FROM python:3.11-slim
WORKDIR /app
COPY app /app
RUN pip install --no-cache-dir paho-mqtt
CMD ["python", "main.py"]
