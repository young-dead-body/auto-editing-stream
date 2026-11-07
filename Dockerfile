FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STREAM_EDITOR_DATA=/data/stream-editor
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-desktop.txt .
RUN pip install --no-cache-dir -r requirements-desktop.txt
COPY app.py transcribe_stream.py highlights_to_fcp7xml.py render_preview.py analyze_highlights.py deep_analysis.py visual_analysis.py device_profile.py export_clips.py ./
COPY web/ ./web/
EXPOSE 8765
CMD ["python", "app.py", "--host", "0.0.0.0", "--no-browser"]
