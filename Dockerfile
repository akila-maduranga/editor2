FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg ca-certificates curl && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://dist.gpac.io/gpac/linux/gpg.asc -o /etc/apt/keyrings/gpac.asc && \
    chmod a+r /etc/apt/keyrings/gpac.asc

RUN . /etc/os-release && \
    echo "Types: deb" > /etc/apt/sources.list.d/gpac.sources && \
    echo "URIs: https://dist.gpac.io/gpac/linux/ubuntu" >> /etc/apt/sources.list.d/gpac.sources && \
    echo "Suites: bookworm" >> /etc/apt/sources.list.d/gpac.sources && \
    echo "Components: nightly" >> /etc/apt/sources.list.d/gpac.sources && \
    echo "Signed-By: /etc/apt/keyrings/gpac.asc" >> /etc/apt/sources.list.d/gpac.sources

RUN apt-get update && apt-get install -y gpac && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p uploads outputs

EXPOSE 5000

CMD ["python", "app.py"]
