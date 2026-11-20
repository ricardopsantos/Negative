# summary: CLIP image and text embeddings for smart search, fully offline (OpenAI CLIP ViT-B/32 as quantized ONNX); pure helpers (rank) need no model.
"""Smart search: a text and a photo are turned into 512 numbers by the same model; the closest photos are the answer.
Reads photos only; the vectors live in the app database. The model files are baked into the image (see Dockerfile)."""
import os
from pathlib import Path

import numpy as np

SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
MIN_SCORE = 0.20   # a photo this far from the words (cosine) is not an answer
KEEP_BELOW_BEST = 0.07   # nor is one much worse than the best match


def models_dir() -> Path:
    return Path(os.environ.get("PHOTOS_MODELS_DIR", "/app/models"))


def available() -> bool:
    d = models_dir()
    return all((d / f).is_file() for f in ("clip_vision_quantized.onnx", "clip_text_quantized.onnx", "clip_tokenizer.json"))


def preprocess(im):
    """PIL image -> float32 [1,3,224,224]: shortest side to 224 (bicubic), centre crop, CLIP mean/std."""
    from PIL import Image
    im = im.convert("RGB")
    w, h = im.size
    r = SIZE / min(w, h)
    im = im.resize((max(SIZE, round(w * r)), max(SIZE, round(h * r))), Image.BICUBIC)
    w, h = im.size
    l, t = (w - SIZE) // 2, (h - SIZE) // 2
    a = np.asarray(im.crop((l, t, l + SIZE, t + SIZE)), np.float32) / 255.0
    return ((a - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)


def rank(matrix, query, limit=250):
    """[(row index, score)] best first: at least MIN_SCORE and within KEEP_BELOW_BEST of the best, at most limit."""
    if matrix is None or len(matrix) == 0:
        return []
    sims = matrix @ query
    order = np.argsort(-sims)[:limit]
    if not len(order):
        return []
    floor = max(MIN_SCORE, float(sims[order[0]]) - KEEP_BELOW_BEST)
    return [(int(i), float(sims[i])) for i in order if sims[i] >= floor]


class Engine:
    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        d = models_dir()
        opt = ort.SessionOptions()
        opt.intra_op_num_threads = int(os.environ.get("PHOTOS_CLIP_THREADS", "3"))
        self.vision = ort.InferenceSession(str(d / "clip_vision_quantized.onnx"), opt, providers=["CPUExecutionProvider"])
        self.text = ort.InferenceSession(str(d / "clip_text_quantized.onnx"), opt, providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(str(d / "clip_tokenizer.json"))
        self.tok.enable_truncation(max_length=77)

    @staticmethod
    def _unit(v):
        v = np.asarray(v, np.float32).reshape(-1)
        return v / (np.linalg.norm(v) or 1.0)

    def image(self, im):
        return self._unit(self.vision.run(None, {"pixel_values": preprocess(im)})[0])

    def words(self, q):
        ids = np.array([self.tok.encode(q).ids], np.int64)
        return self._unit(self.text.run(None, {"input_ids": ids})[0])
