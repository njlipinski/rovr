# rovr.spec — PyInstaller build spec
#
# Build:   pyinstaller rovr.spec
# Output:  dist/rovr.exe
#
# To rebuild after code changes: python -m PyInstaller rovr.spec
#
# config.py is intentionally excluded from the bundle — each machine
# must have its own copy beside rovr.exe. Copy config.example.py and
# edit the paths before running the exe for the first time.

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PyQt6.sip',
        'bcrypt._bcrypt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Exclude config so it is never bundled — it must live beside the exe.
    excludes=['config'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='rovr',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# macOS only — produces dist/rovr.app; ignored on Windows
app = BUNDLE(
    exe,
    name='rovr.app',
    icon=None,
    bundle_identifier='edu.wwu.marsresearchgroup.rovr',
)
