"""Bounded, normalized image handling for marketplace listings."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage

from app.marketplace.constants import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    THUMBNAIL_DIMENSION,
)

IMAGE_NAME_RE = re.compile(r"^[a-f0-9]{32}\.jpg$")
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class ImageValidationError(ValueError):
    """An upload is not a safe, supported image."""


@dataclass(frozen=True)
class ProcessedImage:
    filename: str
    original_filename: str | None
    width: int
    height: int
    path: Path
    thumbnail_path: Path


def process_image(upload: FileStorage, upload_root: Path) -> ProcessedImage:
    """Validate a bounded upload and write normalized JPEG and thumbnail files."""
    raw = upload.stream.read(MAX_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageValidationError("Each image must be smaller than 5 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as probe:
                image_format = probe.format
                if image_format not in ALLOWED_FORMATS:
                    raise ImageValidationError("Upload a JPEG, PNG, or WebP image.")
                if probe.width * probe.height > MAX_IMAGE_PIXELS:
                    raise ImageValidationError("Image dimensions exceed the safe limit.")
                probe.verify()
            with Image.open(BytesIO(raw)) as decoded:
                if decoded.format not in ALLOWED_FORMATS:
                    raise ImageValidationError("Upload a JPEG, PNG, or WebP image.")
                decoded.load()
                oriented = ImageOps.exif_transpose(decoded)
                if oriented.width * oriented.height > MAX_IMAGE_PIXELS:
                    raise ImageValidationError("Image dimensions exceed the safe limit.")
                normalized = _to_rgb(oriented)
                normalized.thumbnail(
                    (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS
                )
                width, height = normalized.size
                thumbnail = normalized.copy()
                thumbnail.thumbnail(
                    (THUMBNAIL_DIMENSION, THUMBNAIL_DIMENSION), Image.Resampling.LANCZOS
                )
    except ImageValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        ValueError,
    ) as exc:
        raise ImageValidationError("The uploaded file is not a valid supported image.") from exc

    upload_root.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}.jpg"
    path = upload_root / filename
    thumbnail_path = upload_root / f"thumb_{filename}"
    try:
        normalized.save(path, format="JPEG", quality=88, optimize=True)
        thumbnail.save(thumbnail_path, format="JPEG", quality=82, optimize=True)
    except OSError as exc:
        path.unlink(missing_ok=True)
        thumbnail_path.unlink(missing_ok=True)
        raise ImageValidationError("The image could not be processed.") from exc

    original_filename = Path(upload.filename or "").name
    original_filename = "".join(char for char in original_filename if char.isprintable())[:160]
    return ProcessedImage(filename, original_filename or None, width, height, path, thumbnail_path)


def _to_rgb(image: Image.Image) -> Image.Image:
    """Convert pixels to RGB so output contains no alpha or metadata."""
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def safe_image_path(upload_root: Path, filename: str, *, thumbnail: bool = False) -> Path | None:
    """Resolve only generated image basenames; never join untrusted paths."""
    if not IMAGE_NAME_RE.fullmatch(filename):
        return None
    stored_name = f"thumb_{filename}" if thumbnail else filename
    candidate = upload_root / stored_name
    if not candidate.is_file() or candidate.parent.resolve() != upload_root.resolve():
        return None
    return candidate


def remove_processed_files(image: ProcessedImage) -> None:
    image.path.unlink(missing_ok=True)
    image.thumbnail_path.unlink(missing_ok=True)


def remove_stored_files(upload_root: Path, filename: str) -> None:
    if IMAGE_NAME_RE.fullmatch(filename):
        (upload_root / filename).unlink(missing_ok=True)
        (upload_root / f"thumb_{filename}").unlink(missing_ok=True)
