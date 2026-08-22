# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


datas = collect_data_files("rapidocr_onnxruntime")
for card_map_name in (
    "card_image_text_map.csv",
    "card_image_text_map.json",
    "card_location_group_map.csv",
    "card_location_group_map.json",
    "misfortune_card_map.csv",
    "misfortune_card_map.json",
):
    if os.path.exists(card_map_name):
        datas.append((card_map_name, "."))
if os.path.isdir("card_images_full"):
    datas.append(("card_images_full", "card_images_full"))
if os.path.isdir("misfortune_card_images"):
    datas.append(("misfortune_card_images", "misfortune_card_images"))
hiddenimports = collect_submodules("rapidocr_onnxruntime")
exe_name = os.environ.get("GREMLINS_BUILD_NAME", "Gremlins_assistance")


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "torchvision", "torchaudio"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    uac_admin=False,
    dpi_aware=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
