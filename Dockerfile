FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data volume — mount here to keep the DB across container restarts
VOLUME /app/data

# Tell the app where to store the database
ENV DB_PATH=/app/data/jre_data.db

EXPOSE 5000

CMD ["python", "server.py"]
