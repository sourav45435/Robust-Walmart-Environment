FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code, config and category taxonomy
COPY . .

# Run playwright install for chromium
RUN playwright install chromium

# Default entrypoint for Azure Container Apps / Azure Functions
CMD ["python", "walmart_scraper.py"]
