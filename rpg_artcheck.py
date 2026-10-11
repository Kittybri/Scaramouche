"""Validate a curated 26-image Harbinger Chronicles art pack (read-only).

Run: python rpg_artcheck.py --root assets/rpg --strict
Use --allow-partial while curating. Never substitutes for human likeness review.
"""
import argparse
import hashlib
from pathlib import Path
from PIL import Image, UnidentifiedImageError

WORLDS = ("horror", "takeover")
RANKS = (11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)


def required_paths():
    paths = [f"worlds/{world}.jpg" for world in WORLDS]
    for world in WORLDS:
        for rank in RANKS:
            names = ("06-scaramouche.jpg", "06-wanderer.jpg") if rank == 6 else (f"{rank:02d}.jpg",)
            paths.extend(f"portraits/{world}/{name}" for name in names)
    return tuple(paths)


def audit_art_pack(root):
    """Return missing, invalid and wrongly duplicated paths; never edit files."""
    root = Path(root)
    missing, invalid, duplicated, hashes = [], [], [], {}
    for relative in required_paths():
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        try:
            if path.stat().st_size > 6_000_000:
                raise ValueError("image exceeds 6 MB")
            with Image.open(path) as image:
                if image.format != "JPEG":
                    raise ValueError("expected JPEG artwork")
                w, h = image.size
                if relative.startswith("worlds/"):
                    if w < 800 or h < 450:
                        raise ValueError("world background is smaller than 800x450")
                elif w < 300 or h < 300:
                    raise ValueError("portrait is smaller than 300x300")
                image.verify()
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError, UnidentifiedImageError, SyntaxError) as exc:
            invalid.append(f"{relative}: {type(exc).__name__}: {exc}")
    for rank in RANKS:
        names = ("06-scaramouche.jpg", "06-wanderer.jpg") if rank == 6 else (f"{rank:02d}.jpg",)
        for name in names:
            left, right = (f"portraits/{world}/{name}" for world in WORLDS)
            if left in hashes and right in hashes and hashes[left] == hashes[right]:
                duplicated.append(f"{name}: corrupted and takeover images are byte-identical")
    for world in WORLDS:
        left = f"portraits/{world}/06-scaramouche.jpg"
        right = f"portraits/{world}/06-wanderer.jpg"
        if left in hashes and right in hashes and hashes[left] == hashes[right]:
            duplicated.append(f"{world}: rank-six bot portraits are byte-identical")
    return {"missing": tuple(missing), "invalid": tuple(invalid),
            "duplicated": tuple(duplicated)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("assets/rpg"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--strict", dest="strict", action="store_true", default=True)
    group.add_argument("--allow-partial", dest="strict", action="store_false")
    args = parser.parse_args()
    results = audit_art_pack(args.root)
    for category, items in results.items():
        print(f"{category}: {len(items)}")
        for item in items:
            print("  " + item)
    failed = bool(results["invalid"] or results["duplicated"] or
                  (args.strict and results["missing"]))
    if not failed:
        print("File checks passed; canonical likeness and horror designs still need human approval.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
