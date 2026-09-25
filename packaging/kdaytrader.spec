# PyInstaller 빌드 설정: pyinstaller packaging/kdaytrader.spec --noconfirm
# 결과: dist/KDayTrader/KDayTrader(.exe) + _internal (onedir: 시작이 빠르고 백신 오탐이 적다)
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hidden = collect_submodules("kdaytrader") + collect_submodules("websockets") + ["tzdata"]
datas = [
    (os.path.join(ROOT, "kdaytrader", "web"), os.path.join("kdaytrader", "web")),
    (os.path.join(ROOT, "kdaytrader", "webui.html"), "kdaytrader"),
    (os.path.join(ROOT, "config.example.yaml"), "."),
]
try:
    from PyInstaller.utils.hooks import collect_data_files
    datas += collect_data_files("tzdata")
except Exception:
    pass

a = Analysis(
    [os.path.join(ROOT, "launcher.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KDayTrader",
    console=True,  # 창을 닫으면 프로그램 종료 · 주소와 오류를 보여 준다
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="KDayTrader", upx=False)
