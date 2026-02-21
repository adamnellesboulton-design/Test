FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tell the app where to store the database.
# On Railway, mount a volume to /app/data to persist the DB across deploys.
ENV DB_PATH=/app/data/jre_data.db

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "server:app"]
