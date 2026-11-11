"""Locate the user's Documents folder.

Windows may redirect Documents (OneDrive Known Folder Move, group policy, a
custom location), in which case ``~/Documents`` is only a leftover stub. Read the
shell folder setting first so downloads land in the folder Explorer shows.
"""

import os

_FALLBACK = os.path.join(os.path.expanduser("~"), "Documents")

_SHELL_FOLDERS = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
)


def documents_dir():
    """Return the real Documents folder, honouring Windows redirection."""
    if os.name != "nt":
        return _FALLBACK
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS) as key:
            value, _ = winreg.QueryValueEx(key, "Personal")
        # The value is usually unexpanded, e.g. %USERPROFILE%\OneDrive\Documents.
        path = os.path.expandvars(value)
        return path if path and os.path.isdir(path) else _FALLBACK
    except Exception:
        return _FALLBACK
