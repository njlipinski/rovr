"""Resolves paths to bundled resource files (icons, etc.).

Works both when running from source and when frozen into a PyInstaller
exe/app bundle, where bundled data lives under sys._MEIPASS instead of
the source tree.
"""
import os
import sys


def resource_path(*parts):
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(__file__))
    return os.path.join(base, *parts)


ICON_PATH = resource_path('app', 'assets', 'ROVRicon.png')
