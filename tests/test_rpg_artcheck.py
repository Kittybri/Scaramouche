"""Code-only tests for the 26-file artwork release gate."""
from PIL import Image
from rpg_artcheck import audit_art_pack, required_paths


def pack(root):
    for index, name in enumerate(required_paths()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        color = (index * 11 % 256, index * 17 % 256, index * 23 % 256)
        Image.new("RGB", (900, 500) if name.startswith("worlds/") else (350, 350),
                  color).save(path, format="JPEG")


def test_no_art_is_allowed_for_work_in_progress_but_not_release(tmp_path):
    missing = audit_art_pack(tmp_path)
    assert len(missing["missing"]) == 26
    assert not missing["invalid"] and not missing["duplicated"]


def test_complete_pack_is_structurally_valid(tmp_path):
    pack(tmp_path)
    assert all(not items for items in audit_art_pack(tmp_path).values())


def test_identical_horror_and_takeover_are_rejected(tmp_path):
    pack(tmp_path)
    source = tmp_path / "portraits/takeover/11.jpg"
    (tmp_path / "portraits/horror/11.jpg").write_bytes(source.read_bytes())
    assert any("11.jpg" in x for x in audit_art_pack(tmp_path)["duplicated"])


def test_rank_six_art_must_be_distinct_for_each_bot(tmp_path):
    pack(tmp_path)
    source = tmp_path / "portraits/horror/06-scaramouche.jpg"
    (tmp_path / "portraits/horror/06-wanderer.jpg").write_bytes(source.read_bytes())
    assert any("rank-six" in x for x in audit_art_pack(tmp_path)["duplicated"])


def test_invalid_and_too_small_images_rejected(tmp_path):
    pack(tmp_path)
    (tmp_path / "portraits/horror/04.jpg").write_bytes(b"not an image")
    Image.new("RGB", (100, 100)).save(tmp_path / "worlds/horror.jpg")
    assert len(audit_art_pack(tmp_path)["invalid"]) == 2
