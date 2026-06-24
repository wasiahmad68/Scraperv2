FROM python:3.10.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Tell Playwright not to download its own Chromium (we use real Chrome)
ENV PLAYWRIGHT_BROWSERS_PATH=/dev/null
# Path to real Chrome for nodriver / Playwright
ENV CHROME_BIN=/usr/bin/google-chrome-stable

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    # Virtual display — lets Chrome run in headed mode without a physical screen
    xvfb xauth \
    # Chrome runtime libraries
    libglib2.0-0 libnss3 libfontconfig1 \
    libx11-xcb1 libxi6 libxrandr2 libxrender1 libxfixes3 \
    libasound2 libcups2 libdbus-1-3 \
    libatk-bridge2.0-0 libatk1.0-0 libgtk-3-0 \
    libxcomposite1 libxdamage1 libxext6 libxss1 \
    && rm -rf /var/lib/apt/lists/*

# ── Google Chrome stable ──────────────────────────────────────────────────────
RUN wget -q -O /tmp/linux_signing_key.pub https://dl.google.com/linux/linux_signing_key.pub \
    && apt-key add /tmp/linux_signing_key.pub \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* /tmp/linux_signing_key.pub

# ── Python packages ───────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .
COPY api.py .
COPY domain_registry.py .

EXPOSE 8000

# Start Xvfb virtual display then launch the FastAPI server.
# xvfb-run hangs in some Docker environments so we start Xvfb directly.
ENV DISPLAY=:99
CMD ["bash", "-c", "Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &>/tmp/xvfb.log & sleep 1 && uvicorn api:app --host 0.0.0.0 --port 8000"]
