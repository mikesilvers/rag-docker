"""The checks of 22_ui_ingest.sh (#641), one per subcommand. Host-side, standard
library, Python 3.9+.

    python3 ui_ingest_check.py cases MANIFEST
    python3 ui_ingest_check.py CHECK CASE MANIFEST RESULTS DUMP
    python3 ui_ingest_check.py cleanup|cleanup-check

MANIFEST is the fixtures' ui_ingest/manifest.json, RESULTS the observations
browser/ui_ingest.js printed (what the pages showed and the API's job records),
DUMP what ui_ingest_weaviate.py read from the verify project's Weaviate. A check
exits 0 when it holds and 1 when it doesn't, the reason on standard error.
Document names and phrases are checked against Weaviate only. `cleanup` deletes
the suite's VfyUiIngest* collections and vfy-ui-* prepared documents through the
API as the harness Engineer; `cleanup-check` fails while any is left.
SPECIFICATIONS.md §10.9.
"""
import json
import sys

import prepare_stack
from auth_stack import role_token

PREFIX = "VfyUiIngest"
DOCUMENT_PREFIX = "vfy-ui-"
TERMINAL = ("completed", "failed", "partial", "cancelled", "interrupted")


class Failed(Exception):
    pass


def expect(ok, reason):
    if not ok:
        raise Failed(reason)


def norm(text):
    """Every run of white space as one space."""
    return " ".join(text.split())


def found(phrase, chunks):
    want = norm(phrase)
    return any(want in norm(chunk) for chunk in chunks)


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as error:
        raise Failed("could not read {}: {}".format(path, error)) from None


class Case:
    def __init__(self, name, manifest, results, dump):
        cases = {c["name"]: c for c in load(manifest)["cases"]}
        expect(name in cases, "no case {!r} in the manifest".format(name))
        self.spec = cases[name]
        self.results = load(results)
        self.obs = self.results["cases"].get(name)
        expect(self.obs is not None, "the browser recorded nothing for this case")
        expect(not self.obs.get("skipped"), "the case was skipped")
        self.collection = self.spec["collection"]
        self.dump = load(dump)["collections"].get(self.collection) if dump else None

    def ran(self):
        """The browser got through every step of the case."""
        expect("failed_step" not in self.obs, "the case stopped at '{}': {}".format(
            self.obs.get("failed_step"), self.obs.get("error")))

    def documents(self):
        expect(self.dump is not None, "Weaviate has no collection {}".format(self.collection))
        return self.dump["documents"]

    def expected(self):
        """{document name: phrase} for the names check; None when the case is split."""
        if self.spec["kind"] == "prepare":
            records = self.obs.get("records") or {}
            out = {}
            for raw, phrase in self.spec["documents"].items():
                parts = (records.get(raw) or {}).get("parts")
                expect(parts, "no prepared parts recorded for {}".format(raw))
                for part in parts:
                    out[part] = phrase
            return out
        return dict(self.spec["documents"])


def page_count(case):
    counts = case.results.get("collections") or {}
    expect(case.collection in counts, "the Collections page doesn't list {}".format(case.collection))
    return counts[case.collection]


def parts_of(name, documents):
    """The part numbers of `name` among the documents; any other name is returned too."""
    numbers, others = [], []
    for doc in documents:
        if doc.startswith(name + " (part ") and doc.endswith(")") and doc[len(name) + 7:-1].isdigit():
            numbers.append(int(doc[len(name) + 7:-1]))
        else:
            others.append(doc)
    return sorted(numbers), others


# ── checks ────────────────────────────────────────────────────────────────────

def check_job(case):
    """The upload was accepted, the page followed its job to `completed` and shows no
    error, and the API's record has no file error."""
    case.ran()
    upload = case.obs.get("upload") or {}
    expect(upload.get("status") == 202, "the upload answered {} {}".format(upload.get("status"), upload.get("body")))
    job_id = (upload.get("body") or {}).get("job_id")
    page = case.obs.get("page") or {}
    expect(job_id and page.get("job_id") == job_id, "the page shows job {}, the upload started {}".format(
        page.get("job_id"), job_id))
    expect(page.get("status") == "completed", "the page shows the job {}".format(page.get("status")))
    expect(not page.get("errors"), "the page lists errors: {}".format(page.get("errors")))
    expect(not page.get("red"), "the page shows: {}".format(page.get("red")))
    record = (case.obs.get("record") or {}).get("body") or {}
    expect(record.get("status") == "completed", "the API's job record is {}".format(record.get("status")))
    expect(record.get("files_failed") == 0 and record.get("errors") == [],
           "the API's job record has files_failed {} and errors {}".format(record.get("files_failed"), record.get("errors")))


def check_contents(case):
    """The Collections page shows at least the case's minimum of chunks."""
    case.ran()
    minimum = case.spec.get("min_chunks") or len(case.expected())
    count = page_count(case)
    expect(count >= minimum, "the Collections page shows {} objects, at least {} expected".format(count, minimum))


def check_names(case):
    """The collection's documents in Weaviate are exactly the expected ones."""
    case.ran()
    documents = sorted(case.documents())
    if "split" in case.spec:
        name = case.spec["split"]["file"]
        numbers, others = parts_of(name, documents)
        expect(not others and len(numbers) >= 2 and numbers == list(range(1, len(numbers) + 1)),
               "the documents are {}, the parts of {} expected".format(documents, name))
        return
    want = sorted(case.expected())
    expect(documents == want, "Weaviate holds {}, expected {}".format(documents, want))


def check_retrieval(case):
    """Every expected document's phrase is in one of its chunks."""
    case.ran()
    documents = case.documents()
    if "split" in case.spec:
        split = case.spec["split"]
        numbers, _ = parts_of(split["file"], documents)
        expect(numbers, "no part of {} in Weaviate".format(split["file"]))
        first = "{} (part {})".format(split["file"], numbers[0])
        last = "{} (part {})".format(split["file"], numbers[-1])
        expect(numbers[0] == 1 and found(split["first"], documents[first]),
               "{!r} is not in {}".format(split["first"], first))
        expect(len(numbers) > 1 and found(split["last"], documents[last]),
               "{!r} is not in {}".format(split["last"], last))
        return
    if case.spec["kind"] == "prepare":
        # A prepared document's phrase may be in any of its parts.
        records = case.obs.get("records") or {}
        missing = [raw for raw, phrase in sorted(case.spec["documents"].items())
                   if not any(found(phrase, documents.get(part, []))
                              for part in (records.get(raw) or {}).get("parts") or [])]
    else:
        missing = [name for name, phrase in sorted(case.expected().items())
                   if not found(phrase, documents.get(name, []))]
    expect(not missing, "no chunk holds the phrase of {}".format(missing))


def check_split(case):
    """The page and the job record name the file split into parts; Weaviate holds parts 1 to N, N >= 2."""
    case.ran()
    name = case.spec["split"]["file"]
    page = case.obs.get("page") or {}
    # Each entry is the API's sentence about one file, starting with its name.
    expect(any(e.startswith(name + ":") for e in page.get("split") or []),
           "the page's split list is {}".format(page.get("split")))
    record = (case.obs.get("record") or {}).get("body") or {}
    expect(any(e.startswith(name + ":") for e in record.get("split") or []),
           "the job record's split is {}".format(record.get("split")))
    numbers, _ = parts_of(name, case.documents())
    expect(len(numbers) >= 2 and numbers == list(range(1, len(numbers) + 1)),
           "the parts in Weaviate are {}".format(numbers))


def check_prepared(case):
    """The preparation showed its job completed with no error, every document prepared."""
    upload = case.obs.get("prepare_upload") or {}
    expect(upload.get("status") == 201, "the Prepare upload answered {} {}".format(upload.get("status"), upload.get("body")))
    prep = case.obs.get("preparation")
    expect(prep is not None, "the case stopped at '{}' before the preparation ended: {}".format(
        case.obs.get("failed_step"), case.obs.get("error")))
    expect(prep.get("shows_job") and prep.get("status") == "completed",
           "the preparation section shows {}".format(prep.get("status")))
    expect(not prep.get("errors"), "the preparation section lists errors: {}".format(prep.get("errors")))
    rows = prep.get("rows") or {}
    for raw in case.spec["documents"]:
        row = rows.get(raw)
        expect(row and row.get("status") == "prepared" and not row.get("last_error"),
               "{}'s row shows {}".format(raw, row))


def check_message(case):
    """The page shows a clear message naming the file and the reason."""
    case.ran()
    page = case.obs.get("page") or {}
    upload = case.obs.get("upload") or {}
    if case.spec["job"]:
        expect(upload.get("status") == 202, "the upload answered {}".format(upload.get("status")))
        expect(page.get("status") == "failed", "the page shows the job {}".format(page.get("status")))
        shown = page.get("errors") or []
        expect(any(e.startswith(case.spec["message"][0]) for e in shown),
               "no error line starts with {!r}: {}".format(case.spec["message"][0], shown))
    else:
        expect(upload.get("status") == 400, "the upload answered {}".format(upload.get("status")))
        shown = page.get("red") or []
        expect(any(all(word in m for word in case.spec["message"]) for m in shown),
               "no red message holds {}: {}".format(case.spec["message"], shown))


def check_nothing_left(case):
    """The collection holds nothing, and no job of it is running (none at all after a refused upload)."""
    case.ran()
    expect(page_count(case) == 0, "the Collections page shows {} objects".format(page_count(case)))
    expect(case.dump is not None and case.dump["count"] == 0,
           "Weaviate holds {}".format(None if case.dump is None else case.dump["count"]))
    expect(case.obs.get("jobs_status") == 200, "the job list answered {}".format(case.obs.get("jobs_status")))
    jobs = case.obs.get("jobs") or []
    running = [j for j in jobs if j["status"] not in TERMINAL]
    expect(not running, "jobs still running: {}".format(running))
    if not case.spec["job"]:
        expect(not jobs, "the refused upload left jobs: {}".format(jobs))


CHECKS = {"job": check_job, "contents": check_contents, "names": check_names,
          "retrieval": check_retrieval, "split": check_split, "prepared": check_prepared,
          "message": check_message, "nothing-left": check_nothing_left}


def browser(manifest, results):
    """The browser recorded every case; a slow case is skipped only under RAG_SKIP_SLOW=1."""
    data = load(results)
    for case in load(manifest)["cases"]:
        obs = data["cases"].get(case["name"])
        expect(obs is not None, "nothing recorded for {}".format(case["name"]))
        if obs.get("skipped"):
            expect(case["slow"] and data.get("skip_slow"), "{} was skipped".format(case["name"]))
    expect(isinstance(data.get("collections"), dict), "the Collections page wasn't read")


def ours(token):
    status, body, _ = prepare_stack.send("GET", "/collections", token)
    expect(status == 200, "GET /collections answered {}".format(status))
    collections = [c["name"] for c in body["collections"] if c["name"].startswith(PREFIX)]
    status, body, _ = prepare_stack.send("GET", "/prepare/documents", token)
    expect(status == 200, "GET /prepare/documents answered {}".format(status))
    documents = [d for d in body["documents"] if d["name"].startswith(DOCUMENT_PREFIX)]
    return collections, documents


def cleanup(check):
    token = role_token("engineer")
    collections, documents = ours(token)
    if check:
        expect(not collections and not documents, "left: {} {}".format(collections, [d["name"] for d in documents]))
        return
    for name in collections:
        prepare_stack.send("DELETE", "/collections/{}?confirm=true".format(name), token)
    for doc in documents:
        prepare_stack.send("DELETE", "/prepare/documents/" + doc["id"], token)


def main(argv):
    try:
        if argv[:1] == ["cases"] and len(argv) == 2:
            for case in load(argv[1])["cases"]:
                kind = "split" if "split" in case else case["kind"]
                print("{}\t{}\t{}".format(kind, "slow" if case["slow"] else "-", case["name"]))
        elif argv[:1] == ["browser"] and len(argv) == 3:
            browser(argv[1], argv[2])
        elif argv[:1] in (["cleanup"], ["cleanup-check"]) and len(argv) == 1:
            cleanup(argv[0] == "cleanup-check")
        elif len(argv) == 5 and argv[0] in CHECKS:
            CHECKS[argv[0]](Case(argv[1], argv[2], argv[3], argv[4]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except Failed as failure:
        print("    " + str(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
