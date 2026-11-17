from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "/Users/",
    "Obsidian Vault",
    "顾知衡",
    "小雨",
    "Wenjun",
    "Harris",
    "62KS-",
    "53EH-",
    "丹桂路",
    ".daddy",
)


def test_repository_has_no_known_private_markers() -> None:
    checked = []
    roots = [ROOT / "src", ROOT / "templates", ROOT / "sample-vault"]
    paths = [ROOT / "README.md", ROOT / "pyproject.toml"]
    paths.extend(path for root in roots for path in root.rglob("*"))
    for path in paths:
        if (
            not path.is_file()
            or path == Path(__file__)
            or path.suffix.lower() in {".png", ".pyc"}
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        checked.append(path)
        for marker in FORBIDDEN:
            assert marker not in text, f"private marker {marker!r} found in {path.relative_to(ROOT)}"
    assert checked
