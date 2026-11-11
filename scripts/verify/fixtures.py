#!/usr/bin/env python3
"""Write the test corpus used by the verification suites.

    python3 fixtures.py <output-dir>

Everything is generated from the standard library so the suites have no
dependencies of their own — the PDF and DOCX are written by hand rather than
pulled from a document library.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import random
import sys
import zipfile

PARAGRAPHS = [
    "Overtime Approval. Requests relating to overtime must be submitted in writing "
    "to the responsible manager, who reviews them within five working days.",
    "Approval for overtime is granted by the department head, except where the "
    "amount exceeds the delegated limit, in which case the finance director approves.",
    "Expense Reimbursement. Employees submit receipts within thirty days of the "
    "expense being incurred. Claims without receipts are refused.",
    "Remote Work. Staff may work remotely up to three days each week with written "
    "agreement from their line manager and the people team.",
    "Records of every decision are retained for seven years in the central archive, "
    "and are available to auditors on request.",
    "Travel Booking. Flights must be booked at least fourteen days in advance. "
    "Rail travel is preferred for journeys under four hours.",
]
ONE_LINER = PARAGRAPHS[0]


def _pdf(paragraphs: list[str], padding: int = 0) -> bytes:
    """A minimal single-page PDF. Hand-built to avoid a writer dependency.

    `padding` adds an unreferenced stream object of that many bytes. No page
    points at it, so parsers skip it: the file is large on the wire but carries
    only the text above, which keeps an upload-size test fast to ingest.
    """
    lines: list[str] = []
    for para in paragraphs:
        cur = ""
        for word in para.split():
            if len(cur) + len(word) + 1 > 82:
                lines.append(cur)
                cur = word
            else:
                cur = (cur + " " + word).strip()
        lines.extend([cur, ""])

    stream = b"BT /F1 11 Tf 54 740 Td 14 TL\n"
    for line in lines:
        safe = (line.encode("ascii", "replace")
                    .replace(b"(", b"").replace(b")", b"").replace(b"\\", b""))
        stream += b"(" + safe + b") Tj T*\n"
    stream += b"ET"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if padding:
        # Seeded so the fixture is byte-identical on every run. Random bytes
        # rather than zeros so nothing on the path can compress it away.
        filler = random.Random(21).randbytes(padding)
        objects.append(b"<< /Length " + str(padding).encode() + b" >>\nstream\n"
                       + filler + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def _docx(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def write(target: pathlib.Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(PARAGRAPHS) + "\n"

    # One of each supported type. `.md` is here because it silently failed to
    # ingest until the `markdown` dependency was added.
    (target / "policies.txt").write_text(body)
    (target / "policies.md").write_text("# Policies\n\n" + body)
    (target / "policies.csv").write_text(
        "policy,detail\n" + "".join(f'p{i},"{p}"\n' for i, p in enumerate(PARAGRAPHS)))
    (target / "policies.json").write_text(json.dumps(
        [{"policy": f"p{i}", "detail": p} for i, p in enumerate(PARAGRAPHS)], indent=2))
    (target / "policies.pdf").write_bytes(_pdf(PARAGRAPHS))
    (target / "policies.docx").write_bytes(_docx(ONE_LINER))

    # Edge cases.
    # Two fragments under any sensible min_chunk_size, for the merge rule.
    (target / "tiny.txt").write_text("Short.\n\nAlso short.\n\n" + ONE_LINER + "\n")
    # Right extension, unparseable content — one bad file must not fail a batch.
    (target / "broken.pdf").write_bytes(b"%PDF-1.4\nnot a real pdf body\n%%EOF\n")
    # Over nginx's 1 MB default request-body limit, which once rejected every
    # real-world PDF with a 413 before the API saw it. See issue #21.
    (target / "large.pdf").write_bytes(_pdf(PARAGRAPHS, padding=3 * 1024 * 1024))
    # Unsupported types, which must be reported rather than dropped in silence.
    (target / "notes.xyz").write_text("unsupported\n")
    (target / "notes.rtf").write_text("also unsupported\n")

    # A ZIP mixing supported and unsupported members.
    with zipfile.ZipFile(target / "batch.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("policies.txt", "policies.md", "policies.pdf",
                     "notes.xyz", "notes.rtf"):
            z.write(target / name, arcname=name)

    # Issue #290. A ZIP cut before its central directory, like an incomplete
    # download: it once ended the upload in a bare HTTP 500.
    with zipfile.ZipFile(target / "truncated.zip", "w", zipfile.ZIP_STORED) as z:
        z.write(target / "policies.txt", arcname="policies.txt")
    data = (target / "truncated.zip").read_bytes()
    (target / "truncated.zip").write_bytes(data[:data.index(b"PK\x01\x02")])
    # Gzipped JSON and JSON Lines members are decompressed and ingested (#290,
    # #291); other gzip types are skipped as unsupported.
    with zipfile.ZipFile(target / "gz_batch.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(target / "policies.json", arcname="policies.json")
        z.writestr("bills/policies2.json.gz",
                   gzip.compress((target / "policies.json").read_bytes(), mtime=0))
        z.writestr("records.jsonl.gz", gzip.compress(b'{"a": 1}\n', mtime=0))
        z.writestr("notes.txt.gz", gzip.compress(b"unsupported\n", mtime=0))

    # Issue #295. Ordinary nested JSON with non-ASCII text, stored as readable
    # `key: value` lines rather than pretty-printed JSON with escaped characters.
    (target / "plain.json").write_text(json.dumps({
        "title": "Remote work policy",
        "body": "Remote work needs written manager approval.",
        "meta": {"author": "Zoë Müller"},
        "tags": ["policy", "remote"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # Issue #291. JSON Lines: one record per line, converted like ordinary JSON,
    # plain and gzipped; a line with no text adds nothing. bad.jsonl has a
    # malformed second line, which fails the whole file.
    records = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (
        {"bill": "HR-1", "title": "Remote work act", "sponsor": {"name": "Zoë Müller"}},
        {},
        {"bill": "HR-2", "title": "Expense filing act", "tags": ["finance", "deadline"]},
    ))
    (target / "records.jsonl").write_text(records, encoding="utf-8")
    (target / "records.jsonl.gz").write_bytes(gzip.compress(records.encode("utf-8"), mtime=0))
    (target / "bad.jsonl").write_text('{"bill": "HR-3"}\n{"bill": }\n{"bill": "HR-4"}\n',
                                      encoding="utf-8")
    # Issue #314. A JSON Lines file holds at most 1,000,000 lines, every line
    # counting: jsonl_many_records.jsonl.gz is 1,000,001 records with no text
    # (about 3 MB decompressed, a few KB gzipped), which fails the file.
    (target / "jsonl_many_records.jsonl.gz").write_bytes(gzip.compress(b"{}\n" * 1_000_001, mtime=0))

    # Issue #298. An upload's ZIPs may expand to at most 2 GiB in all. Both
    # files are about 2 MB. zip_over_declared.zip honestly declares a member of
    # 2,049 MiB of zeros, refused before anything is read (about 3 s to write).
    # zip_over_gzip.zip declares only its 2 MB gzip member, but that member,
    # 33 concatenated gzip streams of 64 MiB of zeros, decompresses to
    # 2,112 MiB: it is refused while its output is counted.
    zeros = bytes(1024 * 1024)
    member = zipfile.ZipInfo("zeros.bin", date_time=(2026, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(target / "zip_over_declared.zip", "w") as z:
        with z.open(member, "w", force_zip64=True) as out:
            for _ in range(2049):
                out.write(zeros)
    stream = gzip.compress(bytes(64 * 1024 * 1024), mtime=0)
    with zipfile.ZipFile(target / "zip_over_gzip.zip", "w", zipfile.ZIP_STORED) as z:
        z.writestr("big.json.gz", stream * 33)

    # Issue #300. JSON files are parsed whole, so their input is limited to
    # 30,000,000 bytes, and a file may keep at most 1,000,000 records.
    # json_over_size.json is valid JSON one byte over the limit (spaces after
    # the value); json_many_records.json is 1,000,001 tiny records (about
    # 4 MB); json_over_size_gz.zip holds a .json.gz member that decompresses
    # to one byte over the limit.
    over = b'{"k": "v"}'
    over += b" " * (30_000_001 - len(over))
    (target / "json_over_size.json").write_bytes(over)
    (target / "json_many_records.json").write_text("[" + ",".join(["[1]"] * 1_000_001) + "]")
    with zipfile.ZipFile(target / "json_over_size_gz.zip", "w", zipfile.ZIP_STORED) as z:
        z.writestr("big.json.gz", gzip.compress(over, mtime=0))
    # Reviewer test (#300): the input limit applies to a plain .json ZIP member.
    # Stored: 30 MB of spaces deflates past the ZIP ratio cap (#28), which would
    # refuse the upload before the job reaches the JSON limit this tests.
    with zipfile.ZipFile(target / "json_over_size_member.zip", "w", zipfile.ZIP_STORED) as z:
        z.writestr("big.json", over)

    # Issue #28. One ZIP may hold at most 5,000 entries, and a member of 1 MiB
    # or more may expand at most 200 times its compressed size.
    # zip_many_members.zip holds 5,001 empty entries (about 500 KB);
    # zip_high_ratio.zip holds 2 MiB of zeros, deflated to a few KB. Written
    # last: 02_ingest.sh regenerates the fixtures when this one is missing.
    with zipfile.ZipFile(target / "zip_many_members.zip", "w", zipfile.ZIP_STORED) as z:
        for i in range(5001):
            z.writestr(f"m{i:04}.txt", b"")
    with zipfile.ZipFile(target / "zip_high_ratio.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.txt", bytes(2 * 1024 * 1024))

    write_ui_ingest(target / "ui_ingest", target)


# Issue #641. 22_ui_ingest.sh ingests these through the web UI, one new
# collection per case, and finds each phrase in the collection's chunks in
# Weaviate. Every phrase is plain words, unique to its document.
UI_PHRASES = {
    "vfy-ui.pdf": "Cobalt heron ledger seven",
    "vfy-ui.docx": "Amber falcon register nine",
    "vfy-ui.txt": "Silver otter journal four",
    "vfy-ui.md": "Crimson lynx almanac two",
    "vfy-ui.csv": "Jade badger inventory six",
    "vfy-ui.json": "Ivory marten catalogue three",
    "vfy-ui.jsonl": "Copper wren dossier eight",
    "mixed/notes.txt": "Violet stoat bulletin one",
    "mixed/guide.md": "Scarlet ibis handbook five",
    "mixed/table.csv": "Indigo vole tally ten",
    "mixed/report.pdf": "Bronze egret survey eleven",
    "mixed/memo.docx": "Teal ferret minute twelve",
    "mixed/records.jsonl": "Olive pika roster thirteen",
    "mixed/bills/bills.json.gz": "Saffron newt docket fourteen",
    "vfy-ui-records.jsonl.gz": "Cerise gecko ledger fifteen",
    "vfy-ui-prep-notes.txt": "Pewter crane quarterly review",
    "vfy-ui-prep-table.csv": "Garnet tapir supply audit",
}
# The large file's phrases, at most 20 characters and repeated back to back,
# so some 50-character chunk holds one whole.
UI_LARGE_FIRST = "Quartz hare alpha"
UI_LARGE_LAST = "Umber yak omega"
UI_PREFIX = "VfyUiIngest"


def _fixed_zip(data: bytes) -> bytes:
    """A ZIP's members again, each dated 1980-01-01, so the bytes are the same
    on every run (_docx dates them with the time of writing)."""
    import io
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            member = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(member, src.read(info))
    return out.getvalue()


def _ui_documents(name: str, phrase: str) -> bytes:
    """One small document of the type `name` names, holding `phrase`."""
    sentence = f"The {phrase} entry is kept with these policies."
    paras = [PARAGRAPHS[0], sentence, PARAGRAPHS[2]]
    ext = name.rsplit(".", 1)[-1] if not name.endswith(".gz") else name[:-3].rsplit(".", 1)[-1]
    if ext == "pdf":
        data = _pdf(paras)
    elif ext == "docx":
        data = _fixed_zip(_docx(" ".join(paras)))
    elif ext == "txt":
        data = ("\n\n".join(paras) + "\n").encode()
    elif ext == "md":
        data = ("# Policies\n\n" + "\n\n".join(paras) + "\n").encode()
    elif ext == "csv":
        data = ("policy,detail\n" + "".join(f'p{i},"{p}"\n' for i, p in enumerate(paras))).encode()
    elif ext == "json":
        data = json.dumps([{"policy": f"p{i}", "detail": p} for i, p in enumerate(paras)],
                          indent=2).encode()
    elif ext == "jsonl":
        data = "".join(json.dumps({"policy": f"p{i}", "detail": p}) + "\n"
                       for i, p in enumerate(paras)).encode()
    else:
        raise ValueError(name)
    return gzip.compress(data, mtime=0) if name.endswith(".gz") else data


def write_ui_ingest(out: pathlib.Path, fixtures: pathlib.Path) -> None:
    """The files and manifest.json of 22_ui_ingest.sh (#641)."""
    out.mkdir(parents=True, exist_ok=True)
    cases = []
    # Every supported type, one new collection each.
    for ext in ("pdf", "docx", "txt", "md", "csv", "json", "jsonl"):
        name = f"vfy-ui.{ext}"
        (out / name).write_bytes(_ui_documents(name, UI_PHRASES[name]))
        cases.append({"name": f"type .{ext}", "kind": "ingest",
                      "collection": UI_PREFIX + ext.capitalize(), "files": [name],
                      "documents": {name: UI_PHRASES[name]}, "min_chunks": 1})
    # A ZIP of mixed types, a .json.gz among its members (a .json.gz is only
    # ingested from a ZIP), and a .jsonl.gz beside it, in one upload.
    members = [m for m in UI_PHRASES if m.startswith("mixed/")]
    with zipfile.ZipFile(out / "vfy-ui-mixed.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for member in members:
            z.writestr(zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0)),
                       _ui_documents(member, UI_PHRASES[member]))
    (out / "vfy-ui-records.jsonl.gz").write_bytes(
        _ui_documents("vfy-ui-records.jsonl.gz", UI_PHRASES["vfy-ui-records.jsonl.gz"]))
    mixed = members + ["vfy-ui-records.jsonl.gz"]
    cases.append({"name": "archives and mixes", "kind": "ingest", "collection": UI_PREFIX + "Mixed",
                  "files": ["vfy-ui-mixed.zip", "vfy-ui-records.jsonl.gz"],
                  "documents": {m: UI_PHRASES[m] for m in mixed}, "min_chunks": len(mixed)})
    # Over 10,000 pieces at the fixed strategy's chunk size 50, so it is split
    # into parts (#621), and a minimum chunk size of 6,000 merges the pieces, so
    # only about a hundred chunks are embedded. A chunk holds at most 6,000
    # characters, hence the minimum count. The first phrase is only in the first
    # paragraph, the last only in the last.
    filler = [(" ".join(PARAGRAPHS) * 2)[:999] + "." for _ in range(560)]
    large = "\n\n".join([" ".join([UI_LARGE_FIRST] * 60)] + filler
                         + [" ".join([UI_LARGE_LAST] * 60)]) + "\n"
    (out / "vfy-ui-large.txt").write_text(large)
    cases.append({"name": "large file", "kind": "ingest", "collection": UI_PREFIX + "Large",
                  "files": ["vfy-ui-large.txt"],
                  "settings": {"strategy": "fixed", "chunk_size": 50, "min_chunk_size": 6000},
                  "documents": {}, "split": {"file": "vfy-ui-large.txt", "first": UI_LARGE_FIRST,
                                             "last": UI_LARGE_LAST},
                  "min_chunks": -(-len("".join(large.split())) // 6000)})
    # Prepared with the local LLM, then imported (#638). The phrase is in three
    # body lines, so the cleaning can't drop every one.
    notes = UI_PHRASES["vfy-ui-prep-notes.txt"]
    (out / "vfy-ui-prep-notes.txt").write_text("\n".join([
        "Facilities Committee Notes",
        "",
        f"The committee opened the {notes} and agreed the agenda for the session.",
        "Members reviewed the cleaning contract and asked for a revised schedule by March.",
        f"The {notes} found the storage rooms in good order and the fire doors working.",
        "The budget for new chairs was approved and the order will be placed next week.",
        f"Actions from the {notes} are assigned to the facilities manager.",
        "The next meeting is on the first Monday of the month in the main hall.",
    ]) + "\n")
    table = UI_PHRASES["vfy-ui-prep-table.csv"]
    (out / "vfy-ui-prep-table.csv").write_text(
        "item,team,note\nchairs,facilities,ordered\nlamps,facilities,fitted\n"
        f"keys,security,{table}\ndesks,facilities,delivered\n")
    cases.append({"name": "prepare and import", "kind": "prepare",
                  "collection": UI_PREFIX + "Prepared",
                  "files": ["vfy-ui-prep-notes.txt", "vfy-ui-prep-table.csv"],
                  "documents": {n: UI_PHRASES[n] for n in ("vfy-ui-prep-notes.txt",
                                                           "vfy-ui-prep-table.csv")},
                  "slow": True})
    # One negative case per refusal: each must show a clear message and leave
    # nothing in its collection and no job running.
    (out / "vfy-ui-broken.pdf").write_bytes((fixtures / "broken.pdf").read_bytes())
    (out / "vfy-ui-notes.xyz").write_text("unsupported\n")
    (out / "vfy-ui-over-limit.zip").write_bytes((fixtures / "zip_over_declared.zip").read_bytes())
    for name, collection, file, words, job in (
            ("broken PDF", "BadPdf", "vfy-ui-broken.pdf", ["vfy-ui-broken.pdf:"], True),
            ("unsupported type", "BadType", "vfy-ui-notes.xyz",
             ["No supported files", "vfy-ui-notes.xyz"], False),
            ("over-limit ZIP", "BigZip", "vfy-ui-over-limit.zip",
             ["vfy-ui-over-limit.zip", "expands to more than"], False)):
        cases.append({"name": name, "kind": "refused", "collection": UI_PREFIX + collection,
                      "files": [file], "message": words, "job": job})
    for case in cases:
        case.setdefault("settings", {})
        case.setdefault("slow", False)
    (out / "manifest.json").write_text(json.dumps({"cases": cases}, indent=2) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fixtures.py <output-dir>")
    out = pathlib.Path(sys.argv[1])
    write(out)
    for path in sorted(out.iterdir()):
        if path.is_file():
            print(f"  {path.name:16s} {path.stat().st_size:7d} bytes")
