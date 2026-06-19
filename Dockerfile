FROM python:3.12-slim

# ffmpeg：mp4(libx264) 编码用，imageio 调它
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8088
# 平台地址/狗身份在运行时给：docker run -e BACKEND_URL=... -e GO2_DEMO_RID=... -e GO2_DEMO_TOKEN=... -p 8088:8088
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8088"]
