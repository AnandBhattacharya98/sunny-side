# Image for LiveKit Cloud agent hosting (`lk agent create` from this folder).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Bake the VAD and turn-detector model files into the image so calls start fast
RUN python -m livekit.agents download-files
CMD ["python", "agent.py", "start"]
