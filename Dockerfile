# Image officielle Playwright : Chromium et ses dépendances déjà présents, utilisateur non root pwuser.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools

# Dossiers d'exécution : montés en volumes, propriété de l'utilisateur non root.
RUN mkdir -p logs traces data uploads state \
    && chown -R pwuser:pwuser /app

USER pwuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/ || exit 1

# --workers 1 obligatoire : mutex et session navigateur sont process-local.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
