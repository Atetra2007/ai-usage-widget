# PyInstaller 打包配置：在仓库根目录运行
#   pyinstaller packaging/AIUsageWidget.spec --noconfirm
# 产物：dist/AIUsageWidget/AIUsageWidget.exe（文件夹模式，启动快、兼容 pythonnet）
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
APP = os.path.join(ROOT, 'app')

a = Analysis(
    [os.path.join(APP, 'main.py')],
    pathex=[APP],
    datas=[(os.path.join(APP, 'ui'), 'ui')],
    hiddenimports=[
        'paths', 'settings',
        'providers', 'providers.common', 'providers.claude', 'providers.codex',
        'providers.local', 'providers.gpu',
        'webview.platforms.winforms', 'webview.platforms.edgechromium', 'clr',
    ],
    excludes=['tkinter', 'unittest', 'pydoc', 'test'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='AIUsageWidget',
    console=False,
    icon=os.path.join(SPECPATH, 'icon.ico'),
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name='AIUsageWidget')
