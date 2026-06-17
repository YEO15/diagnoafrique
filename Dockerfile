# ══════════════════════════════════════════════════════
#  DiagnoAfrique v2.0 — Dockerfile
#  Image légère ~150 MB
#  Build : docker build -t diagnoafrique .
#  Run   : docker run -p 5000:5000 -v $(pwd)/data:/app/data diagnoafrique
# ══════════════════════════════════════════════════════

FROM python:3.11-slim

WORKDIR /app

# Copier requirements en premier (cache Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le projet
COPY . .

# Créer les dossiers nécessaires
RUN mkdir -p uploads templates data

# Variables d'environnement
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# Port Flask
EXPOSE 5000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

# Lancement
CMD ["python", "app.py"]
