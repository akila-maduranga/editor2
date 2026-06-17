FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg ca-certificates curl && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://dist.gpac.io/gpac/linux/gpg.asc -o /etc/apt/keyrings/gpac.asc && \
    chmod a+r /etc/apt/keyrings/gpac.asc

RUN . /etc/os-release && \
    case "${VERSION_CODENAME}" in \
        bullseye|bookworm|jammy|noble) \
            suite="${VERSION_CODENAME}" ;; \
        *) \
            suite="bookworm" ;; \
    esac && \
    printf 'Types: deb\nURIs: https://dist.gpac.io/gpac/linux/%s\nSuites: %s\nComponents: main\nSigned-By: /etc/apt/keyrings/gpac.asc\n' "${ID}" "${suite}" > /etc/apt/sources.list.d/gpac.sources

RUN apt-get update && apt-get install -y gpac && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p uploads outputs

EXPOSE 5000

CMD ["python", "app.py"]
