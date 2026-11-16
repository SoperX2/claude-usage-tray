"""Kontrollerar att PyInstallers bootloader är byggd här och inte är den förbyggda från PyPI.

Bygget sätter PYINSTALLER_COMPILE_BOOTLOADER=1 och installerar pyinstaller med --no-binary, så att
bootloadern kompileras från källkod. Den förbyggda bootloadern finns i otaliga program, även skadliga,
och antivirusprogram känner igen den. Skulle pip ändå installera den förbyggda (t.ex. om flaggan slutar
fungera i en ny pip- eller PyInstaller-version) stoppar det här skriptet bygget i stället för att
releasen tyst får den förbyggda.

Körs i GitHub Actions efter installationen: python scripts/check_bootloader.py
"""

import hashlib
import subprocess
import sys
import tempfile
import zipfile
from importlib.metadata import version
from pathlib import Path

import PyInstaller

# runw.exe är bootloadern för --noconsole, som exe-filen byggs med
BOOTLOADER = "PyInstaller/bootloader/Windows-64bit-intel/runw.exe"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    installed = Path(PyInstaller.__file__).parent.parent / BOOTLOADER
    if not installed.is_file():
        print(f"::error::The bootloader is missing: {installed}")
        return 1
    local = sha256(installed.read_bytes())

    pyinstaller_version = version("pyinstaller")
    with tempfile.TemporaryDirectory() as tmp:
        # Den officiella wheel-filen från PyPI, bara för att jämföra med. Den installeras inte.
        subprocess.run(
            [sys.executable, "-m", "pip", "download", f"pyinstaller=={pyinstaller_version}",
             "--only-binary", ":all:", "--no-deps", "--no-cache-dir", "--dest", tmp],
            check=True,
        )
        wheel = next(Path(tmp).glob("pyinstaller-*.whl"))
        with zipfile.ZipFile(wheel) as zf:
            official = sha256(zf.read(BOOTLOADER))

    print(f"PyInstaller {pyinstaller_version}")
    print(f"Bootloader built here:    {local}")
    print(f"Prebuilt from PyPI:       {official}")
    if local == official:
        print("::error::The bootloader is the prebuilt one from PyPI, not built from source")
        return 1
    print("The bootloader was built from source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
