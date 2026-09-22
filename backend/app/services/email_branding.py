"""Original letterhead from the supplied ARTEL document, embedded in email."""
import base64
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class BrandImage:
    content: bytes
    data_url: str
    content_id: str


@lru_cache(maxsize=1)
def artel_letterhead() -> BrandImage:
    content = (Path(__file__).resolve().parent.parent / "templates" / "artel_letterhead.jpeg").read_bytes()
    return BrandImage(
        content=content,
        data_url="data:image/jpeg;base64," + base64.b64encode(content).decode("ascii"),
        content_id=f"artel-letterhead-{hashlib.sha256(content).hexdigest()[:20]}@fuellead",
    )


def allow_email_image(tag: str, attribute: str, value: str) -> str | None:
    # Only our bundled raster image may use data:. Other URLs remain subject
    # to nh3's scheme checks; arbitrary embedded HTML/SVG is never allowed.
    if value.lstrip().lower().startswith("data:"):
        return value if tag == "img" and attribute == "src" and value == artel_letterhead().data_url else None
    return value


def inline_letterhead(html: str) -> tuple[str, BrandImage | None]:
    image = artel_letterhead()
    found = False
    for quote in ('"', "'"):
        source = f"src={quote}{image.data_url}{quote}"
        if source in html:
            html = html.replace(source, f"src={quote}cid:{image.content_id}{quote}")
            found = True
    return html, image if found else None
