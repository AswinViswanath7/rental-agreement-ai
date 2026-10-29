"""Project sitecustomize: shims for the Python 3.15 / future-package env.

The installed ``filelock`` (>=4) removed ``WeakFileLock`` and the installed
``huggingface_hub`` still imports it. We shim ``WeakFileLock`` (alias of
``SoftFileLock``) into both ``filelock`` and ``huggingface_hub.utils`` at
interpreter startup so ``transformers`` / ``sentence-transformers`` import
cleanly. No document data is touched here.
"""

import sys
import types


def _install_weak_filelock_shim() -> None:
    try:
        import filelock as _fl  # type: ignore
    except Exception:
        return
    if not hasattr(_fl, "WeakFileLock") and hasattr(_fl, "SoftFileLock"):
        _fl.WeakFileLock = _fl.SoftFileLock  # type: ignore[attr-defined]

    # huggingface_hub._local_folder does `from .utils import WeakFileLock`.
    try:
        import huggingface_hub.utils as _hfu  # type: ignore
    except Exception:
        return
    if not hasattr(_hfu, "WeakFileLock"):
        wfl = getattr(_fl, "WeakFileLock", None)
        if wfl is not None:
            setattr(_hfu, "WeakFileLock", wfl)  # type: ignore[attr-defined]


_install_weak_filelock_shim()
