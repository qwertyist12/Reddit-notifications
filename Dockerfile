FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot ./bot

# Watch state lives here; mount a volume so restarts do not re-announce posts.
ENV DATA_DIR=/data
VOLUME ["/data"]

CMD ["python", "-u", "main.py"]
