# Base image - Python 3.11 slim para mantener la imagen ligera
FROM python:3.11-slim

# Evita que Python escriba .pyc y bufferea stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Instalar dependencias del sistema que necesita yfinance y pandas
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python primero
# (se cachea si requirements.txt no cambia — builds más rápidos)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del proyecto
COPY . .

# Crear directorios necesarios en tiempo de build
RUN mkdir -p data logs

# Comando de arranque
CMD ["python", "main.py"]
