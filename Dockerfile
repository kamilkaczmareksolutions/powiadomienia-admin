FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application and service account
COPY main.py .
COPY oauth2service.json .

# Run
CMD ["python", "-u", "main.py"]
