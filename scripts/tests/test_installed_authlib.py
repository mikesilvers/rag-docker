"""The Authlib installed in the API image is the version api/requirements.txt pins (issue #565).

Authlib is only a dependency of weaviate-client (#562), so nothing else would notice an
image whose installed version drifted from the lock. Runs in the built API image with no
network (`01_infrastructure.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_installed_authlib.py

Standard library only. The pin is read from the checkout's api/requirements.txt, so a
re-lock needs no change here.
"""
import importlib.metadata
import re
import unittest
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[2] / "api" / "requirements.txt"


def pinned_version(text, package="authlib"):
    """The `name==version` pin of `package` in a requirements file's text, or None."""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line)
        if match and re.sub(r"[-_.]+", "-", match.group(1)).lower() == package:
            return match.group(2)
    return None


class InstalledAuthlibTests(unittest.TestCase):
    def test_requirements_pin_authlib(self):
        self.assertIsNotNone(pinned_version(REQUIREMENTS.read_text()),
                             "api/requirements.txt has no Authlib==<version> line")

    def test_installed_matches_pin(self):
        pin = pinned_version(REQUIREMENTS.read_text())
        try:
            installed = importlib.metadata.version("Authlib")
        except importlib.metadata.PackageNotFoundError:
            self.fail("Authlib is not installed (api/requirements.txt pins {})".format(pin))
        self.assertEqual(installed, pin,
                         "Authlib {} is installed but api/requirements.txt pins {}".format(installed, pin))

    def test_pinned_version_parser(self):
        text = "# Authlib==0.0.1\nJinja2==3.1.6\nAuthlib==9.9.9  # note\n"
        self.assertEqual(pinned_version(text), "9.9.9")
        self.assertEqual(pinned_version("auth_lib==1.0\nJinja2==3.1.6\n"), None)
        self.assertEqual(pinned_version("AUTHLIB==2.0\n"), "2.0")


if __name__ == "__main__":
    unittest.main()
