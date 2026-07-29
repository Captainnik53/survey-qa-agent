# Playwright's official Python image ships Chromium + all system deps already,
# which is what makes a Streamlit + browser-automation app deployable as one container.
# The tag MUST match the pinned playwright version (1.61.0).
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium   # no-op if already in the base image

COPY . .

ENV PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Container platforms inject $PORT; default to 8501 for local `docker run`.
EXPOSE 8501
CMD streamlit run app.py \
      --server.port=${PORT:-8501} \
      --server.address=0.0.0.0 \
      --server.headless=true \
      --server.enableCORS=false \
      --server.enableXsrfProtection=false
