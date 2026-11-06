FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KIPOFOX_DATA=/data/kipofox
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-desktop.txt .
RUN pip install --no-cache-dir -r requirements-desktop.txt
COPY app.py transcribe_stream.py highlights_to_fcp7xml.py render_preview.py ./
COPY web/ ./web/
EXPOSE 8765
CMD ["python", "app.py", "--host", "0.0.0.0", "--no-browser"]
