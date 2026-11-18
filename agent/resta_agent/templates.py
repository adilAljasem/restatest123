"""قوالب ملفات التشغيل (Docker وnginx وأودو)."""
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "templates"


def render(name: str, values: dict) -> str:
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    if "{{" in text:
        missing = text[text.index("{{"):text.index("{{") + 40]
        raise ValueError(f"template {name}: unresolved placeholder near {missing!r}")
    return text


def write(name: str, dest: Path, values: dict, mode: int = 0o644) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(name, values), encoding="utf-8")
    dest.chmod(mode)
    return dest
