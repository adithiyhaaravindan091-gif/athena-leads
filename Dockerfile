FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cere.py maps-lead-finder.html black.gif ./
RUN playwright install chromium
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
EXPOSE 8000
CMD ["sh","-c","python cere.py --serve --host 0.0.0.0 --port ${PORT:-8000}"]
