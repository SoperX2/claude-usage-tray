"""Skapar assets/icon.ico för exe-filen från samma mätare som appen ritar.

Kör från projektmappen:  python scripts/make_icon.py
Varje storlek ritas för sig, så att även de minsta blir skarpa.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claude_usage_tray import make_app_icon  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 256)
OUT = ROOT / "assets" / "icon.ico"


def main() -> None:
    images = [make_app_icon(size) for size in SIZES]
    OUT.parent.mkdir(exist_ok=True)
    # Pillow använder bilderna i append_images för de storlekar som matchar, i stället för att skala ner
    images[-1].save(OUT, sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print(f"Skrev {OUT.relative_to(ROOT)} ({', '.join(map(str, SIZES))} px)")


if __name__ == "__main__":
    main()
