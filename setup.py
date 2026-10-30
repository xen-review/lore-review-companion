from pathlib import Path
import runpy

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def source_revision():
    root = Path(__file__).resolve().parent
    identity = runpy.run_path(str(root / "lore_review_companion/build_identity.py"))
    stamp = root / "lore_review_companion/_build_identity.py"
    return (
        runpy.run_path(str(stamp))["REVISION"]
        if stamp.exists()
        else identity["checkout_revision"](root)
    )


class StampedBuild(build_py):
    def run(self):
        revision = source_revision()
        super().run()
        target = Path(self.build_lib) / "lore_review_companion/_build_identity.py"
        target.write_text(f"REVISION = {revision!r}\n", encoding="ascii")


class StampedSdist(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        target = Path(base_dir) / "lore_review_companion/_build_identity.py"
        target.write_text(f"REVISION = {source_revision()!r}\n", encoding="ascii")


setup(cmdclass={"build_py": StampedBuild, "sdist": StampedSdist})
