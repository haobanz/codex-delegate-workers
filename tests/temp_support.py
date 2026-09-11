"""Keep every test's disposable workspace inside this repository."""

import os
import tempfile
from pathlib import Path


TEST_TMP = Path(__file__).resolve().parents[1] / "tmp"
# A non-Git fixture inside tmp must not discover and mutate this enclosing repo.
ceilings = os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep)
os.environ["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
    [value for value in ceilings if value] + [str(TEST_TMP)])


def temporary_directory(*, prefix="delegate-test-"):
    directory = TEST_TMP
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or directory.resolve() != directory:
        raise OSError(f"Test temp directory must stay in the repository: {directory}")
    return tempfile.TemporaryDirectory(prefix=prefix, dir=directory)
