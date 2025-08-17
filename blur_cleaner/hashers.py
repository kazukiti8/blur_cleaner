from blake3 import blake3
from PIL import Image, UnidentifiedImageError
import imagehash

def file_hash(path: str, chunk_size=1024*1024) -> str:
    h = blake3()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()

def perceptual_hash(path: str) -> str | None:
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            return str(imagehash.phash(im))
    except (UnidentifiedImageError, OSError):
        return None
