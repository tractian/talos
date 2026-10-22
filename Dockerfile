# Talos Modbus proxy with the 5 MB student model baked in.
#   docker build -t talos .
#   docker run --rm -p 15502:15502 talos --upstream 192.0.2.10:502
FROM python:3.12-slim AS base
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY talos ./talos
RUN pip install --no-cache-dir -e ".[onnx]" huggingface_hub

# Weights: either bake them in at build time (ARG TALOS_HF_REPO) or mount ./weights at run time.
ARG TALOS_HF_REPO=""
RUN if [ -n "$TALOS_HF_REPO" ]; then TALOS_HF_REPO="$TALOS_HF_REPO" talos-pull --dest /app/weights; fi

ENV TALOS_WEIGHTS=/app/weights
EXPOSE 15502
ENTRYPOINT ["talos-proxy", "--listen", "15502"]
CMD ["--upstream", "127.0.0.1:502"]
