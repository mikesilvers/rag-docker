"""Host-only check that the Weaviate image is pinned the same way everywhere (#530).

Reads docker-compose.yml, package-offline.sh, README.md, SPECIFICATIONS.md, the
copies embedded in IMPLEMENTATION.md and .github/dependabot.yml as text. The
compose pin must be a plain release tag: the architecture-suffixed tags
(`1.40.2-9858973.arm64`) carry one platform only. Every other place that states
the pin must name the same tag, and Dependabot must ignore every Weaviate update,
because a version rule cannot keep it off the suffixed tags (#527, #530).

Standard library only; no Docker or stack needed.
Run: python3 scripts/tests/test_weaviate_pin.py
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'semitechnologies/weaviate'
TAG = re.compile(re.escape(IMAGE) + r':([^\s`"\',)]+)')


def read(name):
    return (ROOT / name).read_text()


def compose_tag():
    match = re.search(r'^\s+image: ' + re.escape(IMAGE) + r':(\S+)\s*$', read('docker-compose.yml'), re.M)
    assert match, 'docker-compose.yml pins no ' + IMAGE + ' image'
    return match.group(1)


def section(text, start, end):
    """The text from the line `start` up to the line `end`."""
    begin = text.index('\n' + start + '\n')
    return text[begin:text.index('\n' + end + '\n', begin)]


def embedded(name):
    """The first fenced block under each `### <name>` heading of IMPLEMENTATION.md."""
    text = read('IMPLEMENTATION.md')
    blocks = re.findall(r'^### ' + re.escape(name) + r'\n(?:(?!^### ).)*?^```[a-z]*\n(.*?)^```$', text, re.S | re.M)
    assert blocks, 'IMPLEMENTATION.md embeds no copy of ' + name
    return blocks


def stated_pins():
    """(where, text that states the pin) for each place outside docker-compose.yml."""
    places = [('package-offline.sh IMAGES', re.search(r'^IMAGES=\(\n(.*?)^\)', read('package-offline.sh'),
                                                      re.S | re.M).group(1))]
    spec = read('SPECIFICATIONS.md')
    places.append(('SPECIFICATIONS.md compose copy', '\n'.join(
        line for line in spec.splitlines() if re.match(r'^\s+image: ' + re.escape(IMAGE) + ':', line))))
    places.append(('SPECIFICATIONS.md 11.3 image list', section(spec, '### 11.3 Offline bundle contents',
                                                                '### 11.4 Offline install procedure')))
    readme = read('README.md')
    row = re.search(r'^\| Vector store \| Weaviate (\S+) \|', readme, re.M)
    places.append(('README.md component table', IMAGE + ':' + row.group(1) if row else ''))
    places.append(('README.md docker pull', '\n'.join(
        line for line in readme.splitlines() if line.startswith('docker pull ' + IMAGE + ':'))))
    for name in ('docker-compose.yml', 'package-offline.sh'):
        for number, block in enumerate(embedded(name), 1):
            places.append(('IMPLEMENTATION.md copy {} of {}'.format(number, name), block))
    return places


def entry(text, ecosystem):
    """The lines of one `updates` entry of dependabot.yml."""
    lines = text.splitlines()
    start = lines.index('  - package-ecosystem: "{}"'.format(ecosystem))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith('  - '):
            break
        block.append(line)
    return block


class WeaviatePinTests(unittest.TestCase):
    def test_compose_pins_a_plain_release_tag(self):
        tag = compose_tag()
        self.assertRegex(tag, r'^\d+\.\d+\.\d+$',
                         'an architecture- or build-suffixed tag may carry one platform only')

    def test_every_stated_pin_matches_compose(self):
        tag = compose_tag()
        for where, text in stated_pins():
            with self.subTest(where=where):
                self.assertEqual(sorted(set(TAG.findall(text))), [tag],
                                 '{} names another Weaviate tag than docker-compose.yml'.format(where))

    def test_dependabot_ignores_every_weaviate_update(self):
        block = entry(read('.github/dependabot.yml'), 'docker-compose')
        code = [line for line in block if not line.lstrip().startswith('#')]
        item = '      - dependency-name: "{}"'.format(IMAGE)
        self.assertIn('    ignore:', code)
        self.assertIn(item, code, 'the docker-compose entry does not ignore ' + IMAGE)
        start = code.index(item)
        rest = []
        for line in code[start + 1:]:
            if line.startswith('      - ') or not line.startswith('        '):
                break
            rest.append(line.strip())
        self.assertEqual(rest, [], 'the rule must ignore every version, with no versions or update-types')
        comment = block[block.index(item) - 1]
        self.assertTrue(comment.lstrip().startswith('#'), 'the rule has no comment saying why')


if __name__ == '__main__':
    unittest.main()
