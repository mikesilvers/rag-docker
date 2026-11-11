"""Host-only check that the first-party version strings agree (#605).

Reads the release version from eight places: ui/package.json, the two root
versions in ui/package-lock.json (top-level and packages[""]), the MCPServer
version in mcp/mcpapp.py, the two service.version defaults in
api/services/telemetry.py (Config.version and the SERVICE_VERSION default), the
RAG_OTEL_SERVICE_VERSION default in docker-compose.telemetry.yml and the
RAG_OTEL_SERVICE_VERSION the verify harness sets in
docker-compose.telemetry.verify.yml. It compares them with each other, not with
a fixed version, so a release bumps them together and a partial bump fails. The
1.2.0 bump missed the verify harness value (#656); this would have caught it.

Standard library only; no Docker or stack needed.
Run: python3 scripts/tests/test_version_agreement.py
"""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]

# (file, label, key path)
JSON_VALUES = (
    ('ui/package.json', 'version', ('version',)),
    ('ui/package-lock.json', 'version', ('version',)),
    ('ui/package-lock.json', 'packages[""].version', ('packages', '', 'version')),
)

# (file, label, pattern with one group for the version)
TEXT_VALUES = (
    ('mcp/mcpapp.py', 'MCPServer version',
     r'MCPServer\([^)]*?\bversion="([^"]+)"'),
    ('api/services/telemetry.py', 'Config.version default',
     r'^\s*version: str = "([^"]+)"\s*$'),
    ('api/services/telemetry.py', 'SERVICE_VERSION default',
     r'identifier\("SERVICE_VERSION", "([^"]+)"\)'),
    ('docker-compose.telemetry.yml', 'RAG_OTEL_SERVICE_VERSION default',
     r'^\s*RAG_OTEL_SERVICE_VERSION: "\$\{RAG_OTEL_SERVICE_VERSION:-([^}"]+)\}"\s*$'),
    ('docker-compose.telemetry.verify.yml', 'RAG_OTEL_SERVICE_VERSION',
     r'^\s*RAG_OTEL_SERVICE_VERSION: "?([^"\s]+)"?\s*$'),
)


class VersionAgreementTests(unittest.TestCase):
    def json_value(self, name, keys):
        value = json.loads((ROOT / name).read_text())
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                self.fail('{}: no {}'.format(name, ''.join('[{!r}]'.format(k) for k in keys)))
            value = value[key]
        if not isinstance(value, str):
            self.fail('{}: {} is not a string: {!r}'.format(
                name, ''.join('[{!r}]'.format(k) for k in keys), value))
        return value

    def text_value(self, name, pattern):
        found = re.findall(pattern, (ROOT / name).read_text(), re.MULTILINE)
        if len(found) != 1:
            self.fail('{}: expected one match of {}, found {}'.format(name, pattern, len(found)))
        return found[0]

    def test_first_party_versions_agree(self):
        values = [('{} {}'.format(name, label), self.json_value(name, keys))
                  for name, label, keys in JSON_VALUES]
        values += [('{} {}'.format(name, label), self.text_value(name, pattern))
                   for name, label, pattern in TEXT_VALUES]
        by_value = {}
        for location, value in values:
            by_value.setdefault(value, []).append(location)
        if len(by_value) > 1:
            lines = ['first-party versions disagree (#605):']
            for value, locations in sorted(by_value.items(), key=lambda item: -len(item[1])):
                lines.append('  {}: {}'.format(value, ', '.join(locations)))
            self.fail('\n'.join(lines))


if __name__ == '__main__':
    unittest.main()
