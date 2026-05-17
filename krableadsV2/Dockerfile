# Krab Issuer bot — uses Docker runtime on Render so we can ship the
# tesseract OCR binary needed for pixel-precise phone-number redaction
# in user-uploaded images. Native Python runtimes on Render can't run
# `apt-get install` (no sudo), hence the move to Docker.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/4.00/tessdata

# System dependencies:
# - tesseract-ocr      OCR binary used to find phone-number bounding boxes
# - libtesseract-dev   pytesseract bindings need the shared library
# - poppler-utils      PDF rasterisation helper (PyMuPDF is primary, but
#                      poppler is a useful fallback that some tools assume)
# - libjpeg / zlib     Pillow image decoders (already bundled in slim base
#                      but kept explicit so wheel installs never fall back
#                      to building from source).
# - tini               clean shutdown so SIGTERM from Render reaches the bot
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libtesseract-dev \
        poppler-utils \
        libjpeg62-turbo \
        zlib1g \
        tini \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

COPY . /app

# Render injects $PORT only for web services. The bot is a worker so we
# don't bind a port. Default command is the issuer bot; override
# `dockerCommand` in render.yaml to run the admin dashboard from the
# same image.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]
