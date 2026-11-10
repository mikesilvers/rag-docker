FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    poppler-utils \
    tesseract-ocr \
    libmagic1 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only torch before anything else pulls it in.
#
# sentence-transformers and unstructured both depend on torch, and the default
# PyPI wheel for linux/aarch64 declares the entire NVIDIA CUDA dependency set:
# ~3.3 GB of nvidia-* packages plus ~800 MB of triton. None of it can execute
# without an NVIDIA GPU, so on Apple Silicon (and any CPU-only host) it is 4.1 GB
# of dead weight. Installing from the PyTorch CPU index first satisfies the torch
# requirement, so the pip run below leaves it alone rather than resolving the
# CUDA build from PyPI.
#
# These two pins are the counterpart to requirements.txt, which is a generated
# lock file that deliberately omits torch and torchvision -- their "+cpu" local
# versions are not published on PyPI. Keep the pins here in step with the
# versions recorded in the requirements.txt header when you re-lock.
#
# If a future dependency requires a newer torch than is pinned here, pip will
# satisfy it from PyPI with the CUDA build and the image balloons back to
# ~7.6 GB. Bump these pins rather than dropping them.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch==2.14.0 \
    torchvision==0.29.0

# requirements.txt is a generated lock (see api/requirements.in to change deps).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-cache sentence-transformers model at build time
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Install the spaCy model unstructured's text partitioning loads (#695). Without
# it, unstructured downloads the wheel from GitHub on the first .txt or .md
# ingest, which needs DNS and internet access from inside the container and
# fails on an offline install. The version and SHA-256 are the ones the locked
# unstructured pins (unstructured/nlp/tokenize.py); pip refuses a download whose
# hash differs. Like torch, the model is not in requirements.txt. When a re-lock
# moves unstructured, update both here: the check below fails the build if the
# model is not the version unstructured expects, or does not load.
RUN pip install --no-cache-dir --no-deps \
    "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85" \
 && DO_NOT_TRACK=1 python -c "import sys, importlib.metadata as m, spacy, unstructured.nlp.tokenize as t; have = m.version('en_core_web_sm'); sys.exit(f'en_core_web_sm {have} installed, unstructured expects {t._SPACY_MODEL_VERSION}') if have != t._SPACY_MODEL_VERSION else spacy.load('en_core_web_sm')"

# Default to the pre-cached model at runtime: the image already carries it, so
# the Hugging Face libraries must never dial out on first load. Build-time
# pre-caching above stays unaffected (ENV applies at runtime, not during the RUN).
# DO_NOT_TRACK stops unstructured's usage telemetry, which otherwise sends a
# request when it is imported and on every partition call (#695).
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    DO_NOT_TRACK=1

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
