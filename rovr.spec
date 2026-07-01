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

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('matplotlib') + [('app/assets/ROVRicon.png', 'app/assets')],
    hiddenimports=[
        'PyQt6.sip',
        'bcrypt._bcrypt',
        'app.ui.styles',
        'app.ui.dashboard',
        'app.ui.analyst_dash',
        'app.ui.supervisor_dash',
        'app.migrations',
        'app.migrations.m001_add_flags',
        'app.migrations.m002_status_renumber',
        'app.migrations.m003_drop_assigned_to',
        'matplotlib',
        'matplotlib.pyplot',
        'matplotlib.figure',
        'matplotlib.backends.backend_qtagg',
        'matplotlib.backends.backend_agg',
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
    # PyInstaller converts the PNG to .ico/.icns via Pillow at build time
    # (Pillow is pulled in transitively by matplotlib).
    icon='app/assets/ROVRicon.png',
)

# macOS only — produces dist/rovr.app; ignored on Windows
app = BUNDLE(
    exe,
    name='rovr.app',
    icon='app/assets/ROVRicon.png',
    bundle_identifier='edu.wwu.marsresearchgroup.rovr',
)
