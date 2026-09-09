"""
Image uploads (spec: profile avatars + event cover images). Previously
`avatar_url`/`cover_image_url` existed as schema columns with nothing
that ever wrote a real URL into them — this is the missing write path.

Storage: local disk under `MEDIA_ROOT`, served back out via FastAPI's
StaticFiles mount at `/media` (wired in main.py). This matches "local
storage is fine for dev, don't introduce infrastructure just for the
sake of it" — swapping to S3/R2 later means replacing `_save_to_disk()`
with an upload-to-bucket call and returning the bucket URL instead;
every caller of this endpoint is unaffected since they only ever see a
URL back, never a filesystem path.

Security, specifically because "never trust a client-provided MIME type
alone" was called out explicitly:
  - Extension is derived from a real signature sniff (magic bytes) of
    the uploaded content, not from the filename or the browser-supplied
    Content-Type header, both of which are trivially spoofable.
  - File size is enforced by reading up to MAX_BYTES+1 and rejecting
    anything longer, so a client can't exhaust disk by lying about
    Content-Length.
  - The stored filename is always a fresh random UUID + the sniffed
    extension — the client's original filename is never used for a
    path, which is what prevents path traversal / overwrite attacks.
"""
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status

from app.models import User
from app.deps import get_current_user

router = APIRouter()

MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/tmp/around_media")
MAX_BYTES = 5 * 1024 * 1024  # 5MB

# (magic bytes, extension, mime) — sniffed from the actual content, never trusted from the client
SIGNATURES = [
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"RIFF", "webp", "image/webp"),  # WEBP: 'RIFF'....'WEBP' — checked more precisely below
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
]


def _sniff_image_type(head: bytes) -> tuple[str, str] | None:
    for magic, ext, mime in SIGNATURES:
        if head.startswith(magic):
            if magic == b"RIFF":
                if len(head) >= 12 and head[8:12] == b"WEBP":
                    return ext, mime
                continue
            return ext, mime
    return None


os.makedirs(MEDIA_ROOT, exist_ok=True)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_image(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """
    Returns {"url": "/media/<generated-name>.<ext>"}. The caller then
    PATCHes that URL into /users/me (avatar_url) or includes it in
    POST /events (cover_image_url) — this endpoint only ever stores
    bytes and hands back a reference, it doesn't know or care what the
    image is *for*.
    """
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Image must be under {MAX_BYTES // (1024*1024)}MB")
    if len(content) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")

    sniffed = _sniff_image_type(content[:16])
    if not sniffed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported or unrecognized image format — use JPEG, PNG, WEBP, or GIF")
    ext, mime = sniffed

    # Dimension check — soft dependency on Pillow. If it's not
    # installed, we skip this check rather than fail every upload; add
    # `pillow` to requirements.txt to enable it (not added by default
    # to keep this endpoint's dependency footprint minimal).
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(content))
        img.verify()  # raises if the file is a truncated/corrupt image pretending to have a valid header
        width, height = img.size
        if width > 8000 or height > 8000:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Image dimensions too large (max 8000x8000)")
    except HTTPException:
        raise
    except ImportError:
        pass  # Pillow not installed — file-type sniffing above still guards against non-images
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File isn't a valid image")

    filename = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(MEDIA_ROOT, filename)
    with open(path, "wb") as f:
        f.write(content)

    return {"url": f"/media/{filename}", "content_type": mime, "size_bytes": len(content)}
