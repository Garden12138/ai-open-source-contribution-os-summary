import platform
import shutil
import sys
from pathlib import Path


root = Path(SPECPATH).parent
package = root / "contribos"
datas = [
    *((str(path), "contribos/migrations") for path in (package / "migrations").glob("*.sql")),
    *((str(path), "contribos/web/static") for path in (package / "web/static").iterdir() if path.is_file()),
    (str(package / "sandbox/read_file.py"), "contribos/sandbox"),
]
analysis = Analysis(
    [str(package / "__main__.py")],
    pathex=[str(root)],
    datas=datas,
    hiddenimports=["uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
                   "uvicorn.lifespan.on"],
)
exe = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="contribos-backend",
    console=True,
    bootloader_ignore_signals=False,
    upx=False,
)

machine = platform.machine()
arch = "aarch64" if machine == "arm64" else machine
system = "apple-darwin" if sys.platform == "darwin" else "unknown-linux-gnu"
sidecar = root / "desktop/binaries" / f"contribos-backend-{arch}-{system}"
sidecar.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(exe.name, sidecar)
