"""Parse ``docs/boom.v1.0.docx`` into screenshot <-> API pairs for the
dashboard's **API Docs** tab.

The doc is a per-screen API reference: each screen of the mobile app is paired
with the endpoint that renders it. We walk the document in order and extract, for
every app screenshot, the API request that immediately follows it — the HTTP
method and the endpoint URL. Inline *formula diagrams* (which illustrate a
calculation and have no single endpoint behind them) are not app screenshots, so
they are skipped: a screenshot group only becomes a pair when an HTTP request
follows it before the next heading or image group.

The hyperlink is rendered **origin-relative** (scheme/host/port stripped, e.g.
``/devices/{address}/go``) so that a click lands on the endpoint served by the
*same instance* that serves the dashboard — regardless of whether the dashboard
is reached at ``http://localhost:8000/dashboard`` or
``https://boom.sparkpos.cn/dashboard``. See :meth:`Pair.path`.

Pure standard library (``zipfile`` + ``xml.etree``); no third-party ``.docx``
dependency. The docx lives at ``docs/boom.v1.0.docx`` (overridable via the
``BOOM_DOCX`` environment variable, matching the ``DATABASE_URL`` /
``UPLOAD_DIR`` convention of a CWD-relative runtime default).

At build time the docx is the only input: ``tools/extract_boom_images.py`` (via
:func:`extract_images` and :func:`save_pairs`) writes both the screenshot
binaries and the pair metadata into the committed asset dir
``src/dashboard/resources`` — the PNGs and a ``pairs.json``. The dashboard loads
everything from there (:func:`load_pairs`, :func:`image_path`), so the runtime
never reads the docx; the docx exists only to regenerate the assets.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# A top-level section heading (一、二、…接口文档) and a sub-section heading
# (1.1 获取…); both terminate the "look ahead for the request" scan.
_SECTION = re.compile(r"^[一二三四五六七八九十]+、")
_SUBSECTION = re.compile(r"^\d+\.\d+\s")
_METHOD = re.compile(r"(GET|POST|PUT|PATCH|DELETE)", re.IGNORECASE)
_URL = re.compile(r"https?://\S+")

DEFAULT_DOCX = "docs/boom.v1.0.docx"
# Committed asset dir + its manifest, next to this module (src/dashboard/resources).
RESOURCES_DIR = "resources"
PAIRS_FILE = "pairs.json"


def docx_path() -> Path:
    """Path to the source docx: ``$BOOM_DOCX`` if set, else
    ``docs/boom.v1.0.docx`` relative to the current working directory."""
    env = os.environ.get("BOOM_DOCX")
    return Path(env) if env else Path(DEFAULT_DOCX)


@dataclass(frozen=True)
class Pair:
    """One app screen and the endpoint that renders it.

    Attributes:
        section: the top-level section the screen belongs to (e.g. ``一、首页接口文档``).
        title: the sub-section title (e.g. ``1.1 获取首页训练状态``).
        method: the HTTP method (e.g. ``GET``).
        url: the endpoint URL exactly as written in the doc (includes its host).
        images: the media basenames (e.g. ``image1.png``) that screenshot the
            screen; one screen may have more than one shot.
    """

    section: str
    title: str
    method: str
    url: str
    images: tuple[str, ...]

    @property
    def path(self) -> str:
        """Origin-relative href (path + query, no scheme/host/port).

        Clicking this link from the dashboard resolves against the dashboard's
        own origin, so it lands on the endpoint served by the same instance.
        """
        parts = urlsplit(self.url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def to_dict(self) -> dict:
        """JSON-serialisable form (``images`` as a list)."""
        d = asdict(self)
        d["images"] = list(self.images)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Pair:
        return cls(
            section=d["section"],
            title=d["title"],
            method=d.get("method", ""),
            url=d["url"],
            images=tuple(d["images"]),
        )


def fill_address(path: str, address: str | None) -> str:
    """Fill the ``{address}`` template placeholder in an origin-relative path
    (:meth:`Pair.path`) with a concrete device MAC, for a rendered link.

    ``address`` is the MAC of the device selected in the dashboard's global
    sidebar state. When it is ``None`` (no device selected) the ``{address}``
    placeholder is left in place, so the link still shows the template; paths
    without the placeholder (e.g. ``/app/questionnaire``) come back unchanged.
    """
    return path.replace("{address}", address) if address else path


def _rid_targets(z: zipfile.ZipFile) -> dict[str, str]:
    """Map relationship Id -> target (e.g. ``rId5 -> media/image1.png``)."""
    rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    out: dict[str, str] = {}
    for rel in re.finditer(r"<Relationship\s+([^>]*)/>", rels):
        attrs = dict(re.findall(r'(\w+)="([^"]+)"', rel.group(1)))
        out[attrs.get("Id")] = attrs.get("Target", "")
    return out


def _media_index(z: zipfile.ZipFile) -> dict[str, str]:
    """Map media basename -> in-zip path (e.g. ``image1.png -> word/media/…``)."""
    return {
        n.rsplit("/", 1)[-1]: n for n in z.namelist() if n.startswith("word/media/")
    }


def _para_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(_W + "t")).strip()


def _para_images(p: ET.Element, rid2target: dict[str, str]) -> list[str]:
    blips = p.findall(".//" + _A + "blip")
    if not blips:
        return []
    out: list[str] = []
    for b in blips:
        embed = b.get(_R + "embed")
        out.append(rid2target.get(embed, embed or "").rsplit("/", 1)[-1])
    return out


def _blocks(z: zipfile.ZipFile):
    """Flatten the body to an ordered ``(kind, value)`` stream: ``TXT`` runs of
    paragraph text and ``IMG`` runs of screenshot media basenames."""
    rid2target = _rid_targets(z)
    body = ET.fromstring(z.read("word/document.xml")).find(_W + "body")
    blocks: list[tuple[str, str]] = []
    for p in body:
        if p.tag != _W + "p":
            continue
        for img in _para_images(p, rid2target):
            blocks.append(("IMG", img))
        txt = _para_text(p)
        if txt:
            blocks.append(("TXT", txt))
    return blocks


def extract_pairs(path: os.PathLike | str | None = None) -> list[Pair]:
    """Return every screenshot <-> API pair, in document order.

    A screenshot group (one or more consecutive images) becomes a pair when an
    HTTP request — a method and a URL — follows it before the next heading or the
    next image group. Groups with no following request (formula diagrams) are
    skipped.
    """
    z = zipfile.ZipFile(path or docx_path())
    blocks = _blocks(z)
    pairs: list[Pair] = []
    section = ""
    title = ""
    i = 0
    n = len(blocks)
    while i < n:
        kind, val = blocks[i]
        if kind == "TXT":
            if _SECTION.match(val):
                section = val
            elif _SUBSECTION.match(val):
                title = val
            i += 1
            continue
        # A group of consecutive screenshots.
        j = i
        images: list[str] = []
        while j < n and blocks[j][0] == "IMG":
            images.append(blocks[j][1])
            j += 1
        # Scan forward for the request that renders these screenshots, stopping
        # at the next image group or the next heading.
        method = ""
        url = ""
        k = j
        while k < n:
            kk, vv = blocks[k]
            if kk == "IMG" or _SUBSECTION.match(vv) or _SECTION.match(vv):
                break
            if not method:
                m = _METHOD.search(vv)
                if m:
                    method = m.group(1).upper()
            u = _URL.search(vv)
            if u:
                url = u.group(0)
                if not method:
                    m = _METHOD.search(vv)
                    if m:
                        method = m.group(1).upper()
                break
            k += 1
        if url:
            pairs.append(Pair(section, title, method, url, tuple(images)))
        i = j
    return pairs


def resources_dir() -> Path:
    """The committed screenshot asset dir next to this module
    (``src/dashboard/resources``)."""
    return Path(__file__).resolve().parent / RESOURCES_DIR


def image_path(name: str) -> Path:
    """Path to screenshot ``name`` in the resources dir (may not exist yet)."""
    return resources_dir() / name


def extract_images(
    path: os.PathLike | str | None = None,
    dest: os.PathLike | str | None = None,
    prune: bool = False,
    pairs: list[Pair] | None = None,
) -> list[Path]:
    """Copy every screenshot referenced by a pair into ``dest`` (default:
    :func:`resources_dir`). Returns the paths written, in pair order.

    ``path`` is the source docx (used to locate the image bytes); pass ``pairs``
    to reuse a parse already done for :func:`save_pairs` instead of re-parsing.
    Safe to re-run. With ``prune=True``, any ``image*.png``/``image*.svg`` in
    ``dest`` that no pair references is removed, keeping the asset dir in sync
    with the docx.
    """
    if pairs is None:
        pairs = extract_pairs(path)
    referenced = {name for p in pairs for name in p.images}
    dest = Path(dest) if dest is not None else resources_dir()
    dest.mkdir(parents=True, exist_ok=True)
    z = zipfile.ZipFile(path or docx_path())
    index = _media_index(z)
    written: list[Path] = []
    for p in pairs:
        for name in p.images:
            out = dest / name
            if name not in index:
                continue
            out.write_bytes(z.read(index[name]))
            written.append(out)
    if prune:
        for stale in dest.glob("image*"):
            if stale.name not in referenced and stale.suffix in {".png", ".svg"}:
                stale.unlink()
    return written


def pairs_path(dest: os.PathLike | str | None = None) -> Path:
    """Path to the pair manifest (``pairs.json``) — in ``dest`` (default:
    :func:`resources_dir`). This is the runtime asset; the docx is not read."""
    return (Path(dest) if dest is not None else resources_dir()) / PAIRS_FILE


def save_pairs(pairs: list[Pair], path: os.PathLike | str | None = None) -> Path:
    """Write ``pairs`` to the manifest (default: :func:`pairs_path`). Returns
    the path written. UTF-8, ``ensure_ascii=False`` so the Chinese section
    headings stay readable.
    """
    out = Path(path) if path is not None else pairs_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([p.to_dict() for p in pairs], ensure_ascii=False, indent=2)
    out.write_text(payload + "\n", encoding="utf-8")
    return out


def load_pairs(path: os.PathLike | str | None = None) -> list[Pair]:
    """Load pairs from the manifest (default: :func:`pairs_path`).

    This is the runtime path: it reads the committed ``pairs.json`` and never
    touches the docx. Raises :class:`OSError` if the manifest is missing.
    """
    p = Path(path) if path is not None else pairs_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    return [Pair.from_dict(d) for d in data]
