"""Phase Y.4 — face detection + ArcFace embedding on Apple Silicon.

Detection runs on Apple's Vision framework (Neural Engine) via PyObjC.
Recognition runs InsightFace's ArcFace (buffalo_l) ONNX model through
onnxruntime with the CoreMLExecutionProvider. Ported from the accelerator's
`ml/src/models/face_{detect,embed}.py` — see those files for provenance.

Output shape matches what Immich's server writes: per face, a
`boundingBox` in pixel coords with top-left origin, a confidence `score`,
optional 5-point `landmarks` for landmark-aligned alignment, and a
512-dim L2-normalized float32 embedding. Caller (process.py) UPSERTs
into `asset_face` + `face_search` with `sourceType='machine-learning'`.

Heavy imports (`Vision`, `insightface`, `onnxruntime`, `cv2`) are lazy so
`import immy.faces` stays cheap on machines missing any of them — only
`detect` / `embed_faces` fail, and only when actually invoked. See
docs/IMMICH-INGEST.md §1.6 and §4.4.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np


ARCFACE_INPUT_SIZE = 112
ARCFACE_EMBEDDING_DIM = 512
DEFAULT_MODEL = "buffalo_l"


@dataclass
class DetectedFace:
    """One face as returned from Vision, before alignment/embedding.

    Bounding box in image-pixel coords (top-left origin). `landmarks` is
    the 5-point list (left_eye, right_eye, nose, left_mouth, right_mouth)
    when Vision produced a full landmark set; otherwise None and we fall
    back to a bbox crop for alignment.
    """

    x1: int
    y1: int
    x2: int
    y2: int
    score: float
    landmarks: list[list[float]] | None = None


@dataclass
class EmbeddedFace:
    """A DetectedFace plus its 512-dim ArcFace embedding (L2-normalized)."""

    face: DetectedFace
    embedding: np.ndarray  # shape (512,), dtype float32


class FacesUnavailable(RuntimeError):
    """Raised when PyObjC/Vision or insightface/onnxruntime aren't installed.
    Caller decides whether to skip the asset or abort the run."""


# --- Detection (Apple Vision framework) -----------------------------------


def detect(image_bytes: bytes) -> tuple[list[DetectedFace], int, int]:
    """Detect faces in `image_bytes`. Returns (faces, width, height).

    Uses `VNDetectFaceLandmarksRequest` — single pass that yields both
    bounding boxes and 5-point landmarks when the face is frontal enough.
    Coordinates are converted out of Vision's normalized, bottom-left
    origin into image-pixel, top-left origin.
    """
    try:
        from PIL import Image  # noqa: WPS433 — lazy by design
    except ImportError as e:
        raise FacesUnavailable("Pillow is required for face detection") from e
    try:
        pil = Image.open(io.BytesIO(image_bytes))
        width, height = pil.size
    except Exception as e:
        raise ValueError(f"invalid image data: {e}") from e

    try:
        from Foundation import NSAutoreleasePool, NSData  # type: ignore
        import Vision  # type: ignore
    except ImportError as e:
        raise FacesUnavailable(
            "pyobjc-framework-Vision is required for face detection"
        ) from e

    # NSAutoreleasePool keeps long-running processes from leaking the
    # per-request Vision objects — matches the accelerator's pattern.
    pool = NSAutoreleasePool.alloc().init()
    try:
        ns_data = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
            ns_data, None,
        )
        request = Vision.VNDetectFaceLandmarksRequest.alloc().init()
        success, err = handler.performRequests_error_([request], None)
        if not success or err:
            return [], width, height
        faces: list[DetectedFace] = []
        for obs in request.results() or []:
            bbox = obs.boundingBox()
            x1 = bbox.origin.x * width
            y1 = (1.0 - bbox.origin.y - bbox.size.height) * height
            x2 = (bbox.origin.x + bbox.size.width) * width
            y2 = (1.0 - bbox.origin.y) * height
            landmarks = _extract_landmarks(
                obs.landmarks(), bbox, width, height,
            ) if obs.landmarks() else None
            faces.append(DetectedFace(
                x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2),
                score=float(obs.confidence()),
                landmarks=landmarks,
            ))
        return faces, width, height
    finally:
        del pool


def _extract_landmarks(landmarks, face_bbox, img_w: int, img_h: int):
    """Pull 5-point landmarks from Vision and map into image pixel coords.

    Vision's `normalizedPoints` are in face-bbox-relative space (0..1)
    with bottom-left origin. The bbox itself is in image-normalized space,
    also bottom-left origin. We convert both to image pixels, top-left.
    Returns None if any of the five regions is missing.
    """
    bx, by = face_bbox.origin.x, face_bbox.origin.y
    bw, bh = face_bbox.size.width, face_bbox.size.height

    def to_px(nx: float, ny: float) -> list[float]:
        ix = bx + nx * bw
        iy = by + ny * bh
        return [ix * img_w, (1.0 - iy) * img_h]

    def region_points(region) -> list:
        if region is None:
            return []
        n = region.pointCount()
        if n == 0:
            return []
        raw = region.normalizedPoints()
        return [raw[i] for i in range(n)]

    def region_center(region) -> Optional[list[float]]:
        pts = region_points(region)
        if not pts:
            return None
        xs = sum(p.x for p in pts) / len(pts)
        ys = sum(p.y for p in pts) / len(pts)
        return to_px(xs, ys)

    try:
        left_eye = region_center(landmarks.leftEye())
        right_eye = region_center(landmarks.rightEye())
        nose_pts = region_points(landmarks.nose())
        nose = to_px(nose_pts[-1].x, nose_pts[-1].y) if nose_pts else None
        lips = region_points(landmarks.outerLips())
        if lips:
            lips_px = [to_px(p.x, p.y) for p in lips]
            left_mouth = min(lips_px, key=lambda p: p[0])
            right_mouth = max(lips_px, key=lambda p: p[0])
        else:
            left_mouth = right_mouth = None
        if all((left_eye, right_eye, nose, left_mouth, right_mouth)):
            return [left_eye, right_eye, nose, left_mouth, right_mouth]
    except Exception:
        return None
    return None


# --- Embedding (InsightFace ArcFace via onnxruntime CoreML) ----------------


_recognition_model: Any = None
_recognition_model_name: str | None = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def _get_recognition_model(model_name: str):
    """Load and cache the InsightFace recognition model (thread-safe)."""
    global _recognition_model, _recognition_model_name
    with _model_lock:
        if _recognition_model is not None and _recognition_model_name == model_name:
            return _recognition_model
        try:
            import onnxruntime as ort  # type: ignore
            from insightface.model_zoo import model_zoo  # type: ignore
            from insightface.utils.storage import download as download_pack  # type: ignore
        except ImportError as e:
            raise FacesUnavailable(
                "insightface + onnxruntime are required for face embeddings"
            ) from e

        providers = list(ort.get_available_providers())
        if "CoreMLExecutionProvider" in providers:
            providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]

        rec_path = _ensure_recognition_pack(model_name, download_pack)
        _recognition_model = model_zoo.get_model(str(rec_path), providers=providers)
        _recognition_model_name = model_name
        return _recognition_model


def _ensure_recognition_pack(model_name: str, download_pack) -> Path:
    """Find (or download) the ArcFace ONNX within `~/.insightface/models/<model_name>/`."""
    root = Path.home() / ".insightface"
    pack = root / "models" / model_name
    found = _find_arcface(pack) if pack.exists() else None
    if found is not None:
        return found
    download_pack("models", model_name, force=pack.exists(), root=str(root))
    found = _find_arcface(pack)
    if found is None:
        raise FileNotFoundError(
            f"no ArcFace model (3x112x112 → 512) found in {pack}",
        )
    return found


def _find_arcface(pack_dir: Path) -> Path | None:
    """Pick the ArcFace recognition ONNX out of a buffalo_* model pack."""
    import onnxruntime as ort  # type: ignore

    candidates = list(pack_dir.glob("*.onnx"))
    known = ("w600k", "w300k", "glintr", "arcface")
    ranked = [f for f in candidates if any(p in f.name.lower() for p in known)]
    ranked.extend(f for f in candidates if f not in ranked)
    for f in ranked:
        try:
            sess = ort.InferenceSession(str(f), providers=["CPUExecutionProvider"])
            ish = sess.get_inputs()[0].shape
            osh = sess.get_outputs()[0].shape
            if (
                len(ish) == 4 and ish[1] == 3
                and ish[2] == ARCFACE_INPUT_SIZE and ish[3] == ARCFACE_INPUT_SIZE
                and len(osh) == 2 and osh[1] == ARCFACE_EMBEDDING_DIM
            ):
                return f
        except Exception:
            continue
    return None


def _use_per_face_inference(model: Any, batch_size: int) -> bool:
    """Avoid noisy ORT/CoreML shape warnings on fixed-batch ArcFace models.

    Some buffalo_* packs advertise output shape `[1, 512]`. onnxruntime's
    CoreML EP can still execute larger batches, but it emits a
    `VerifyOutputSizes` warning because the runtime output shape differs from
    the model metadata. When we detect that fixed batch-1 shape and the caller
    wants more than one face, run one inference per face instead.
    """
    if batch_size <= 1:
        return False
    shape = getattr(model, "output_shape", None)
    if not isinstance(shape, (list, tuple)) or len(shape) != 2:
        return False
    return shape[0] == 1 and shape[1] == ARCFACE_EMBEDDING_DIM


def embed_faces(
    image_bytes: bytes,
    faces: list[DetectedFace],
    model_name: str = DEFAULT_MODEL,
) -> list[EmbeddedFace]:
    """Run ArcFace over every detected face. Returns one EmbeddedFace per
    successful alignment; faces that fail alignment are dropped.

    Batches the whole image's faces into a single ONNX call — for N faces
    this is 1 decode + 1 inference instead of N of each.
    """
    if not faces:
        return []
    try:
        import cv2  # type: ignore
        from insightface.utils import face_align  # type: ignore
    except ImportError as e:
        raise FacesUnavailable(
            "opencv-python-headless + insightface are required for embeddings"
        ) from e

    nparr = np.frombuffer(image_bytes, np.uint8)
    img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        raise ValueError("failed to decode image bytes for ArcFace alignment")

    aligned: list[np.ndarray | None] = []
    for face in faces:
        try:
            if face.landmarks is not None:
                kps = np.array(face.landmarks, dtype=np.float32)
                aligned.append(face_align.norm_crop(
                    img_bgr, kps, image_size=ARCFACE_INPUT_SIZE,
                ))
            else:
                aligned.append(_bbox_crop(img_bgr, face, cv2))
        except Exception:
            aligned.append(None)

    valid = [i for i, a in enumerate(aligned) if a is not None]
    if not valid:
        return []

    model = _get_recognition_model(model_name)
    batch = [aligned[i] for i in valid]
    with _inference_lock:
        if _use_per_face_inference(model, len(batch)):
            raw = np.concatenate(
                [
                    np.asarray(model.get_feat(face_img), dtype=np.float32).reshape(1, -1)
                    for face_img in batch
                ],
                axis=0,
            )
        else:
            raw = model.get_feat(batch)
    if raw.ndim == 1:
        raw = raw.reshape(1, -1)

    results: list[EmbeddedFace] = []
    for batch_idx, face_idx in enumerate(valid):
        emb = raw[batch_idx].flatten().astype(np.float32)
        norm = float(np.linalg.norm(emb))
        if norm > 0:
            emb = emb / norm
        results.append(EmbeddedFace(face=faces[face_idx], embedding=emb))
    return results


def _bbox_crop(img_bgr, face: DetectedFace, cv2) -> np.ndarray | None:
    """Fallback alignment when Vision didn't produce 5-point landmarks.

    Pads the bbox 10% on each side, clamps to the image, resizes to 112 —
    same recipe the accelerator uses for its bbox fallback.
    """
    w = face.x2 - face.x1
    h = face.y2 - face.y1
    pad_x = int(w * 0.1)
    pad_y = int(h * 0.1)
    x1 = max(0, face.x1 - pad_x)
    y1 = max(0, face.y1 - pad_y)
    x2 = min(img_bgr.shape[1], face.x2 + pad_x)
    y2 = min(img_bgr.shape[0], face.y2 + pad_y)
    crop = img_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (ARCFACE_INPUT_SIZE, ARCFACE_INPUT_SIZE))


def to_pgvector_literal(embedding: np.ndarray) -> str:
    """Render a 512-dim float32 embedding as a pgvector text literal."""
    return "[" + ",".join(f"{float(x):.7g}" for x in embedding) + "]"


__all__ = [
    "ARCFACE_EMBEDDING_DIM",
    "DEFAULT_MODEL",
    "DetectedFace",
    "EmbeddedFace",
    "FacesUnavailable",
    "detect",
    "embed_faces",
    "to_pgvector_literal",
]
