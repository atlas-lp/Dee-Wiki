FROM python:3.11-slim

WORKDIR /app

# libgomp1 is required by faiss-cpu
RUN apt-get update && apt-get install -y libgomp1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime assets — baked in from git working tree
COPY api/     ./api/
COPY data/    ./data/
COPY Vault/   ./Vault/

# Pre-download fastembed model at build time so Cloud Run startup never hits the network.
RUN python -c "\
from fastembed import TextEmbedding; \
TextEmbedding('BAAI/bge-base-en-v1.5', cache_dir='/app/models')" \
  && echo "fastembed model cached."

# Pre-build wiki search FAISS index so Cloud Run startup is instant.
# Reads Vault/wiki/*.md, encodes with the cached fastembed model, and writes
# data/wiki_search.faiss + data/wiki_search_slugs.json into the image.
# After each wiki update that triggers a rebuild, this step re-encodes automatically.
RUN python -c "\
import sys; sys.path.insert(0, '/app/api'); \
from kb import KnowledgeBase; \
print('[Build] Pre-building wiki search FAISS index...'); \
KnowledgeBase(); \
print('[Build] Wiki FAISS cache ready.')"

ENV PORT=8080
EXPOSE 8080

CMD ["python", "api/index2.py", "--host", "0.0.0.0", "--port", "8080"]
