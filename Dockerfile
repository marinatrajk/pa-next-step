FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt
# the knowledge base's embedding model, fetched now so the first search isn't slow
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warm up'])"

COPY . .
RUN chmod +x start.sh
EXPOSE 8080
CMD ["./start.sh"]
