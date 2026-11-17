"""A scanned (image-only) PDF is read by OCR with the locked Pillow (#522, #524).

The Pillow 13 bump was checked by hand for this path only. This draws a page with
Pillow, saves it as an image-only PDF and parses it with the ingest pipeline's own
`_parse_file`, the path every uploaded PDF takes; poppler, tesseract and the locked
Pillow are installed in the API image, not on the host. It also checks that the
installed Pillow is the version api/requirements.txt pins. Runs in the built API image
with no network (`02_ingest.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_scanned_pdf_ocr.py
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', '/app'))

from test_installed_authlib import pinned_version  # noqa: E402  (the script's own directory)

REQUIREMENTS = Path(__file__).resolve().parents[2] / 'api' / 'requirements.txt'
LINES = ('Scanned refund policy', 'Refunds take fourteen days')


class ScannedPdfTests(unittest.TestCase):
    def test_installed_pillow_is_the_pinned_one(self):
        import PIL
        pin = pinned_version(REQUIREMENTS.read_text(), 'pillow')
        self.assertIsNotNone(pin, 'api/requirements.txt has no pillow==<version> line')
        self.assertEqual(PIL.__version__, pin,
                         'Pillow {} is installed but api/requirements.txt pins {}'.format(PIL.__version__, pin))

    def test_image_only_pdf_is_read_by_ocr(self):
        from PIL import Image, ImageDraw, ImageFont
        from services.ingest_pipeline import _parse_file
        page = Image.new('L', (1700, 2200), 255)
        draw = ImageDraw.Draw(page)
        font = ImageFont.load_default(size=64)
        for index, line in enumerate(LINES):
            draw.text((150, 300 + 150 * index), line, fill=0, font=font)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scanned.pdf'
            page.save(path, 'PDF', resolution=200)
            self.assertNotIn(b'/Font', path.read_bytes(), 'the fixture must hold no text layer')
            text, elements = _parse_file(path)
        self.assertTrue(elements, 'OCR returned no elements')
        flat = ' '.join(text.split()).lower()
        for line in LINES:
            self.assertIn(line.lower(), flat)


if __name__ == '__main__':
    unittest.main()
