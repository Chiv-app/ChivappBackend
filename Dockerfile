# ==========================================
# Dockerfile - Chivapp Backend
# ==========================================
FROM python:3.12-slim

WORKDIR /app

# 1. Dependencias del sistema requeridas para postgres, imágenes y PDFs
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libpq5 \
    libjpeg62-turbo \
    libjpeg-dev \
    zlib1g \
    zlib1g-dev \
    libfreetype6 \
    libfreetype6-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2. Instalar dependencias de Python directamente en el entorno del sistema
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Limpiar paquetes de desarrollo para reducir el tamaño de la imagen
RUN apt-get purge -y --auto-remove build-essential libpq-dev libjpeg-dev zlib1g-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

# 4. Crear usuario de seguridad no-root
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --gid 1001 appuser

# 5. Copiar código fuente
COPY --chown=appuser:appgroup . /app

# 6. Directorio para uploads
RUN mkdir -p /app/uploads && chown -R appuser:appgroup /app/uploads

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 2"]
