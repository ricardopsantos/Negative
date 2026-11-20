"""Face detection and recognition, fully offline (OpenCV YuNet detector + SFace recognizer; ONNX models, MIT and Apache-2.0).
Reads photos only; results live in the app database. Pure helpers (assign, merge_centroid) need no OpenCV."""
import os
from pathlib import Path

import numpy as np

SAME_PERSON = 0.40   # cosine similarity of a face to a person's average that counts as "the same person"
MIN_FACE_PX = 40     # smaller faces (at 1280 px on the long side) are background people: ignored
MIN_SHOW = 3         # an unnamed group needs this many photos before it is listed under People
DET_SCORE = 0.8
WORK_PX = 1280


def models_dir() -> Path:
    return Path(os.environ.get("PHOTOS_MODELS_DIR", "/app/models"))


def available() -> bool:
    d = models_dir()
    return (d / "face_detection_yunet_2023mar.onnx").is_file() and (d / "face_recognition_sface_2021dec.onnx").is_file()


def assign(centroids, emb, threshold=SAME_PERSON):
    """(index, similarity) of the best person for emb, or (None, best similarity) if nobody is close enough."""
    if centroids is None or len(centroids) == 0:
        return None, 0.0
    sims = np.asarray(centroids) @ emb
    k = int(np.argmax(sims))
    return (k if sims[k] >= threshold else None), float(sims[k])


def merge_centroid(c1, n1, c2, n2):
    v = np.asarray(c1) * n1 + np.asarray(c2) * n2
    return (v / (np.linalg.norm(v) or 1.0)).astype(np.float32)


class Engine:
    def __init__(self):
        import cv2
        cv2.setNumThreads(int(os.environ.get("PHOTOS_FACE_THREADS", "4")))
        d = models_dir()
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(str(d / "face_detection_yunet_2023mar.onnx"), "", (320, 320), DET_SCORE, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(d / "face_recognition_sface_2021dec.onnx"), "")

    def detect(self, path):
        """[(x, y, w, h, score, embedding float32[128])], the box as fractions of the (upright) image."""
        from PIL import Image, ImageOps
        cv2 = self.cv2
        with Image.open(path) as im:
            if im.format == "JPEG":
                im.draft("RGB", (WORK_PX * 2, WORK_PX * 2))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((WORK_PX, WORK_PX))
            bgr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, found = self.det.detect(bgr)
        out = []
        for r in (found if found is not None else []):
            if r[2] < MIN_FACE_PX or r[3] < MIN_FACE_PX:
                continue
            e = self.rec.feature(self.rec.alignCrop(bgr, r))[0].astype(np.float32)
            e /= np.linalg.norm(e) or 1.0
            out.append((max(0.0, r[0] / w), max(0.0, r[1] / h), min(1.0, r[2] / w), min(1.0, r[3] / h), float(r[14]), e))
        return out
