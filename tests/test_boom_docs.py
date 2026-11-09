"""Dashboard API-docs tab parser (``dashboard.boom_docs``) tests.

The parser turns ``docs/boom.v1.0.docx`` (a per-screen API reference) into
screenshot <-> endpoint pairs for the "API Docs" tab. These tests pin the
extraction contract against the real docx that ships in the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dashboard import boom_docs

# The docx lives at <repo>/docs/boom.v1.0.docx; resolve it from this test file so
# the suite is CWD-independent (the module's own default is CWD-relative).
DOCX = Path(__file__).resolve().parents[1] / "docs" / "boom.v1.0.docx"


def _pairs():
    return boom_docs.extract_pairs(DOCX)


# --- extraction contract ----------------------------------------------------


def test_pair_count_and_first():
    pairs = _pairs()
    assert len(pairs) == 18
    first = pairs[0]
    assert first.title == "1.1 获取首页训练状态"
    assert first.method == "GET"
    assert first.path == "/devices/{address}/go"
    assert first.images == ("image1.png",)


def test_every_pair_has_one_or_more_screenshots():
    assert all(p.images for p in _pairs())


def test_multi_screenshot_pairs():
    # 3.1 sleep, 5.1 load, 7.1 body, 9.1 questionnaire each carry two shots.
    by_title = {p.title: p for p in _pairs()}
    two = [t for t, p in by_title.items() if len(p.images) == 2]
    assert len(two) == 4
    assert "image4.png" in by_title["3.1 获取睡眠页面基础数据"].images
    assert "image23.png" in by_title["5.1 获取负荷页面基础数据"].images
    assert "image39.png" in by_title["9.1 获取问卷内容"].images


def test_paths_are_origin_relative():
    # The link must inherit the dashboard's host, so no scheme/host/port.
    for p in _pairs():
        assert "://" not in p.path
        assert p.path.startswith("/")


def test_questionnaire_uses_app_prefix():
    pairs = [p for p in _pairs() if "questionnaire" in p.path]
    assert [p.path for p in pairs] == ["/app/questionnaire"]


def test_formula_diagrams_are_excluded():
    # Inline formula diagrams are not app screenshots, so no pair references them.
    formula = {
        "image6.png",
        "image8.png",
        "image10.png",
        "image12.png",
        "image14.png",
        "image16.png",
        "image24.png",
        "image28.png",
        "image30.png",
    }
    paired = {n for p in _pairs() for n in p.images}
    assert formula.isdisjoint(paired)
    # …but the 22 real app screenshots are all paired.
    assert len(paired) == 22


# --- extraction into the resources dir ------------------------------------


def test_extract_images_writes_all_pairs(tmp_path):
    pairs = _pairs()
    written = boom_docs.extract_images(DOCX, tmp_path)
    names = {n for p in pairs for n in p.images}
    assert len(written) == 22
    on_disk = set(tmp_path.glob("image*.png"))
    assert on_disk == {tmp_path / n for n in names}
    for f in on_disk:
        assert f.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature


def test_extract_images_is_idempotent(tmp_path):
    first = boom_docs.extract_images(DOCX, tmp_path)
    second = boom_docs.extract_images(DOCX, tmp_path)
    assert [str(p) for p in first] == [str(p) for p in second]
    assert len(first) == 22


def test_extract_images_prune_removes_unreferenced(tmp_path):
    boom_docs.extract_images(DOCX, tmp_path)
    stale = tmp_path / "image99.png"
    stale.write_bytes(b"\x89PNG\r\n\x1a\n")
    boom_docs.extract_images(DOCX, tmp_path, prune=True)
    assert not stale.exists()
    # …but the 22 referenced screenshots survive the prune.
    assert len(list(tmp_path.glob("image*.png"))) == 22


def test_image_path_and_resources_dir():
    # The dashboard loads screenshots from the committed asset dir next to the
    # module; the file we committed must be present so the tab renders.
    assert boom_docs.resources_dir().name == "resources"
    assert (
        boom_docs.image_path("image1.png") == boom_docs.resources_dir() / "image1.png"
    )
    assert boom_docs.image_path("image1.png").is_file()


# --- pair manifest (pairs.json) -------------------------------------------


def test_save_load_pairs_roundtrip(tmp_path):
    pairs = _pairs()
    out = boom_docs.save_pairs(pairs, tmp_path / "pairs.json")
    assert out == tmp_path / "pairs.json"
    loaded = boom_docs.load_pairs(out)
    assert [p.to_dict() for p in loaded] == [p.to_dict() for p in pairs]
    # images survive as tuples (not JSON lists); links stay origin-relative.
    assert loaded[0].images == ("image1.png",)
    assert all("://" not in p.path for p in loaded)


def test_load_pairs_missing_manifest_raises(tmp_path):
    with pytest.raises(OSError):
        boom_docs.load_pairs(tmp_path / "nope.json")


def test_committed_manifest_has_pairs():
    # The runtime loads pairs from the committed manifest, never the docx.
    pairs = boom_docs.load_pairs()
    assert len(pairs) == 18
    assert pairs[0].path == "/devices/{address}/go"


# --- path resolution --------------------------------------------------------


def test_missing_docx_raises(tmp_path):
    with pytest.raises(OSError):
        boom_docs.extract_pairs("/no/such/file.docx")
    with pytest.raises(OSError):
        boom_docs.extract_images("/no/such/file.docx", tmp_path)


def test_docx_path_env_override(monkeypatch):
    monkeypatch.setenv("BOOM_DOCX", "custom/path.docx")
    assert boom_docs.docx_path().as_posix() == "custom/path.docx"


def test_fill_address_substitutes_selected_device_mac():
    # The dashboard fills the {address} template with the selected device's MAC,
    # so the link points at that device's endpoint.
    assert (
        boom_docs.fill_address("/devices/{address}/go", "D9:4C:37:91:9C:7E")
        == "/devices/D9:4C:37:91:9C:7E/go"
    )


def test_fill_address_none_keeps_template():
    # No device selected -> the {address} placeholder stays literal, so the link
    # still shows the endpoint template.
    assert (
        boom_docs.fill_address("/devices/{address}/go", None)
        == "/devices/{address}/go"
    )


def test_fill_address_leaves_non_device_paths_untouched():
    # A path without the placeholder (e.g. the questionnaire) is returned as-is.
    assert (
        boom_docs.fill_address("/app/questionnaire", "D9:4C:37:91:9C:7E")
        == "/app/questionnaire"
    )
