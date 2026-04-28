from pathlib import Path

from brain.pages.parser import parse_page


def regenerate_index(brain_root: Path) -> None:
    """Regenerate the markdown page index under pages/index.md."""
    pages_dir = Path(brain_root) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    page_paths = sorted(
        path
        for path in pages_dir.glob("**/*.md")
        if path.is_file() and path.relative_to(pages_dir).parts[0] not in {"index.md", "log.md"}
    )

    lines = ["# Page Index", ""]
    for page_path in page_paths:
        page = parse_page(page_path)
        relative_path = page_path.relative_to(pages_dir).as_posix()
        lines.append(f"- [{page.frontmatter.title}]({relative_path})")

    _write_lf(pages_dir / "index.md", "\n".join(lines) + "\n")


def append_log(brain_root: Path, message: str) -> None:
    """Append one message line to pages/log.md."""
    pages_dir = Path(brain_root) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    log_path = pages_dir / "log.md"
    line = message.rstrip("\n")

    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        separator = "" if existing.endswith("\n") else "\n"
        text = f"{existing}{separator}{line}\n"
    else:
        text = f"{line}\n"
    _write_lf(log_path, text)


def _write_lf(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
