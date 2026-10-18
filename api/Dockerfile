FROM python:3.14-slim

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

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
