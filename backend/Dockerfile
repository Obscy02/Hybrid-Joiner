FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Azure App Service for Containers sets $PORT itself; default to 8000 for
# any other host that doesn't. DATABASE_URL and DASHBOARD_TOKEN are read
# from the environment (see db.py / auth.py) - not baked into the image,
# so the same image works for any tenant/deployment.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
