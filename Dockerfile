# summary: Photos image: gunicorn (gunicorn.conf.py) serving server.py on :9556 as uid 1000; compose mounts the library read-write only for delete-to-trash (PH18).
FROM python:3.12-slim
LABEL org.opencontainers.image.source=https://github.com/ricardopsantos/Negative org.opencontainers.image.licenses=MIT
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
# tzdata: dates of videos and of files without EXIF are stored as local time (TZ from compose)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --no-deps reverse_geocoder==1.5.1
# Face models pinned to an opencv_zoo commit and checked by sha256: a changed upstream file fails the build
ARG ZOO=https://github.com/opencv/opencv_zoo/raw
RUN mkdir -p /app/models && cd /app/models \
 && python -c "import urllib.request as u; u.urlretrieve('$ZOO/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet/face_detection_yunet_2023mar.onnx', 'face_detection_yunet_2023mar.onnx'); u.urlretrieve('$ZOO/ba91a3b91d00d76e86540d4013f944bd6b514e39/models/face_recognition_sface/face_recognition_sface_2021dec.onnx', 'face_recognition_sface_2021dec.onnx')" \
 && printf '%s  %s\n' 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 face_detection_yunet_2023mar.onnx \
      0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79 face_recognition_sface_2021dec.onnx | sha256sum -c -
# Smart-search model (OpenAI CLIP ViT-B/32, quantized ONNX by Xenova), pinned to a Hugging Face commit and checked by sha256
ARG CLIPREV=6b7c523703ae26e39be4b03774966f8571c7f523
ARG HF=https://huggingface.co/Xenova/clip-vit-base-patch32/resolve
RUN cd /app/models \
 && python -c "import urllib.request as u; b='$HF/$CLIPREV/'; u.urlretrieve(b+'onnx/vision_model_quantized.onnx', 'clip_vision_quantized.onnx'); u.urlretrieve(b+'onnx/text_model_quantized.onnx', 'clip_text_quantized.onnx'); u.urlretrieve(b+'tokenizer.json', 'clip_tokenizer.json')" \
 && printf '%s  %s\n' 583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299 clip_vision_quantized.onnx \
      73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a clip_text_quantized.onnx \
      f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd clip_tokenizer.json | sha256sum -c -
COPY server.py convert.py faces.py places.py clip.py thumbhash.py index.html app.js sw.js favicon.png icon-192.png icon-512.png gunicorn.conf.py logfilter.py ./
# PH77: the map's Leaflet + world outline, served locally (vendor/README.txt)
COPY vendor/ vendor/
# the Immich OpenAPI spec (AGPL, so downloaded, not shipped): /api/users/me/preferences builds its answer from it (PH1).
# Same URL and sha256 as tools/fetch_immich_spec.py (tests/test_spec_pin.py)
RUN mkdir -p /app/docs && cd /app/docs \
 && python -c "import urllib.request as u; u.urlretrieve('https://raw.githubusercontent.com/immich-app/immich/v3.3.6/open-api/immich-openapi-specs.json', 'immich-openapi-v3.3.6.json')" \
 && printf '%s  %s\n' b7f2a9c2543472a9b3a2f32ece82f56cb4b5660d6745d05f2d7903dfb5ba974b immich-openapi-v3.3.6.json | sha256sum -c -
RUN useradd --create-home --uid 1000 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 9556
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:9556/healthz', timeout=8)"
CMD ["gunicorn", "-c", "gunicorn.conf.py", "server:app"]
