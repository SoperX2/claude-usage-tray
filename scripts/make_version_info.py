"""Skapar PyInstallers versionsfil (--version-file) från __version__ i claude_usage_tray.py.

Versionsinformationen syns under Egenskaper → Information i Utforskaren, och Windows visar
FileDescription som avsändare i notiser i stället för "Python". Ett program med ifyllda uppgifter
flaggas också mer sällan av antivirusprogram än ett helt tomt.

Kör: python scripts/make_version_info.py build/version_info.txt
"""

import ast
import re
import sys
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(__file__).resolve().parent.parent
AUTHOR = "Dante Åberg-Bustamante"     # samma som i LICENSE


def read_version() -> str:
    """Läser __version__ utan att importera appen, på samma sätt som release.yml."""
    tree = ast.parse((ROOT / "claude_usage_tray.py").read_text("utf-8"))
    return next(
        n.value.value for n in tree.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "__version__"
    )


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """'1.0.1' -> (1, 0, 1, 0). Windows kräver fyra heltal. Formatet kontrolleras först, så att
    t.ex. '1.1.0rc1' ger det här felet i stället för ett obegripligt fel från int()."""
    error = f"__version__ måste vara 1–4 heltal 0–65535 med punkter, t.ex. 1.0.1: {version!r}"
    if not re.fullmatch(r"\d+(\.\d+){0,3}", version):
        raise ValueError(error)
    parts = [int(p) for p in version.split(".")]
    if any(p > 0xFFFF for p in parts):
        raise ValueError(error)
    return tuple(parts + [0] * (4 - len(parts)))


def build(version: str) -> VSVersionInfo:
    numbers = version_tuple(version)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable("040904B0", [     # engelska (USA), Unicode
                StringStruct("CompanyName", AUTHOR),
                StringStruct("FileDescription", "Claude Usage"),
                StringStruct("FileVersion", version),
                StringStruct("InternalName", "ClaudeUsage"),
                StringStruct("LegalCopyright", f"Copyright (c) 2026 {AUTHOR}. MIT License."),
                StringStruct("OriginalFilename", "ClaudeUsage.exe"),
                StringStruct("ProductName", "Claude Usage"),
                StringStruct("ProductVersion", version),
                StringStruct("Comments", "Unofficial, not affiliated with Anthropic"),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/version_info.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    version = read_version()
    out.write_text(str(build(version)), encoding="utf-8")
    print(f"Skrev {out} för version {version}")


if __name__ == "__main__":
    main()
