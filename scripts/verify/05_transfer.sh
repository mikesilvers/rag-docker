#!/usr/bin/env bash
# RAG_EXPORT_SPECIFICATIONS.md §13 — export, import and tuning (E5-E20, E23, E26, E27)
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
C="${PREFIX}Transfer"
EXPORTS="$REPO_ROOT/exports"

section "Export, import and tuning"

(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_session_import.py)
check "evaluation import and generated-session regressions" $?
python3 "$REPO_ROOT/scripts/tests/test_session_implementation.py"
check "embedded session/import verification sources match" $?

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_t.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_t.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null
chunks_before=$(api_get "/collections" | python3 -c "
import json,sys; print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")

# Retrieval settings must exist for the package to carry retrieve.py.
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":6,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null

# ── export ───────────────────────────────────────────────────────────────────
api_post "/export" "{\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_exp.json
ejob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_exp.json'))['job_id'])")
estatus=$(wait_for_job "/export/job/$ejob" 1800)
check_eq "export completes" "$estatus" "completed"
api_get "/export/job/$ejob" > /tmp/vfy_expjob.json
read -r PKG fidelity script <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_expjob.json'))
print(d['filename'], d['fidelity'], d['retrieve_script'])")"
check_eq "a collection with retained sources exports with-sources" "$fidelity" "with-sources"
check_eq "a tuned collection ships retrieve.py" "$script" "True"

python3 ./validate_package.py "$EXPORTS/$PKG" > /tmp/vfy_val.txt 2>&1
check "package satisfies every §4 clause" $? "$(tail -2 /tmp/vfy_val.txt | head -1)"

# ── corruption is detected ───────────────────────────────────────────────────
python3 - "$EXPORTS/$PKG" <<'ENDPY'
import pathlib, shutil, subprocess, sys, tarfile, tempfile
src = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work)
    root = next(p for p in work.iterdir() if p.is_dir())
    chunks = root / "chunks.jsonl"
    chunks.write_bytes(chunks.read_bytes()[: len(chunks.read_bytes()) // 2])
    out = src.parent / (src.name.replace(".tar.gz", "") + "-corrupt.tar.gz")
    with tarfile.open(out, "w:gz") as t:
        t.add(root, arcname=root.name)
ENDPY
CORRUPT=$(python3 - "$EXPORTS/$PKG" <<'ENDPY'
import pathlib, sys
src = pathlib.Path(sys.argv[1])
print(src.name.replace(".tar.gz", "") + "-corrupt.tar.gz")
ENDPY
)
api_post "/import" "{\"filename\":\"$CORRUPT\",\"on_conflict\":\"abort\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
code=$(api_get "/import/job/$ijob" | jfield "['error_code']")
check_eq "a truncated package is refused as PACKAGE_CORRUPT" "$code" "PACKAGE_CORRUPT"
api_get "/import/job/$ijob" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if 'chunks.jsonl' in (d.get('error') or '') else 1)"
check "the corruption error names the offending file" $?
rm -f "$EXPORTS/$CORRUPT"

# ── evaluation metadata is validated before mutation (E23) ──────────────────
# Add one evaluation sidecar to a copy of the package and re-sign the manifest,
# so the archive is digest-valid and only the session metadata decides.
GS_SID="gs_$(python3 -c 'import uuid;print(uuid.uuid4().hex[:8])')"
make_gs_pkg() {   # make_gs_pkg <session-id> <suffix>; prints the new filename
  python3 - "$EXPORTS/$PKG" "$1" "$2" "$C" <<'ENDPY'
import hashlib, json, pathlib, sys, tarfile, tempfile
src, sid, suffix, coll = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
session = {"session_id": sid, "collection": coll, "status": "completed",
           "pairs_total": 1, "pairs_attempted": 1, "pairs_completed": 1, "pairs_failed": 0,
           "pairs": [{"pair_id": "p_0123abcd", "question": "Q?", "answer": "A",
                      "ground_truth": "A", "contexts": ["C"], "source_file": "policies.txt",
                      "chunk_index": 0, "status": "approved"}]}
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work, filter="data")
    root = next(p for p in work.iterdir() if p.is_dir())
    (root / "goldstandard").mkdir(exist_ok=True)
    side = root / "goldstandard" / "session.json"
    side.write_text(json.dumps(session))
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"]["goldstandard/session.json"] = \
        "sha256:" + hashlib.sha256(side.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    out = src.parent / (src.name.replace(".tar.gz", "") + f"-{suffix}.tar.gz")
    with tarfile.open(out, "w:gz") as t:
        t.add(root, arcname=root.name)
    print(out.name)
ENDPY
}
count_of() { api_get "/collections" | python3 -c "
import json,sys; print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$1'][0])"; }

BADGS=$(make_gs_pkg "not-a-generated-id" badgs)
api_post "/import" "{\"filename\":\"$BADGS\",\"on_conflict\":\"replace\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
api_get "/import/job/$ijob" > /tmp/vfy_gsjob.json
check_eq "E23: malformed evaluation metadata is refused as PACKAGE_CORRUPT" \
  "$(jfield "['error_code']" < /tmp/vfy_gsjob.json)" "PACKAGE_CORRUPT"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_gsjob.json'))
sys.exit(0 if 'goldstandard/session.json' in json.dumps(d) else 1)"
check "E23: the refusal names the offending sidecar" $?
check_eq "E23: replace left the existing collection untouched" "$(count_of "$C")" "$chunks_before"
code=$(api_code "$API/goldstandard/session/not-a-generated-id")
check_eq "E23: no session was restored from the refused package" "$code" "404"
rm -f "$EXPORTS/$BADGS"

# A refused package must change no live state at all. A replace from this
# package would restore the same chunks, so the count above can't tell; the
# retrieval setting and the earlier, valid sidecar can.
MIX_SID="gs_$(python3 -c 'import uuid;print(uuid.uuid4().hex[:8])')"
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":7,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
MIXGS=$(python3 - "$EXPORTS/$PKG" "$MIX_SID" "$C" <<'ENDPY'
import hashlib, json, pathlib, sys, tarfile, tempfile
src, sid, coll = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def session(i, **over):
    s = {"session_id": i, "collection": coll, "status": "completed",
         "pairs_total": 1, "pairs_attempted": 1, "pairs_completed": 1, "pairs_failed": 0,
         "pairs": [{"pair_id": "p_0123abcd", "question": "Q?", "answer": "A",
                    "ground_truth": "A", "contexts": ["C"], "source_file": "policies.txt",
                    "chunk_index": 0, "status": "approved"}]}
    s.update(over); return s
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work, filter="data")
    root = next(p for p in work.iterdir() if p.is_dir())
    gold = root / "goldstandard"; gold.mkdir(exist_ok=True)
    manifest = json.loads((root / "manifest.json").read_text())
    # a_valid sorts first: a restore that isn't preflighted writes it before
    # it reaches the bad one.
    for name, body in (("a_valid.json", session(sid)),
                       ("b_bad.json", session("gs_0000beef", pairs_total="many"))):
        (gold / name).write_text(json.dumps(body))
        manifest["files"][f"goldstandard/{name}"] = \
            "sha256:" + hashlib.sha256((gold / name).read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    out = src.parent / (src.name.replace(".tar.gz", "") + "-mixgs.tar.gz")
    with tarfile.open(out, "w:gz") as t:
        t.add(root, arcname=root.name)
    print(out.name)
ENDPY
)
api_post "/import" "{\"filename\":\"$MIXGS\",\"on_conflict\":\"replace\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
api_get "/import/job/$ijob" > /tmp/vfy_gsjob.json
check_eq "E23: a schema-invalid sidecar after a valid one is refused as PACKAGE_CORRUPT" \
  "$(jfield "['error_code']" < /tmp/vfy_gsjob.json)" "PACKAGE_CORRUPT"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_gsjob.json'))
sys.exit(0 if 'goldstandard/b_bad.json' in json.dumps(d) else 1)"
check "E23: the refusal names the invalid sidecar, not the valid one" $?
code=$(api_code "$API/goldstandard/session/$MIX_SID")
check_eq "E23: the valid sidecar in a refused package is not restored" "$code" "404"
topk=$(api_get "/retrieval/config/$C" | jfield "['top_k']")
check_eq "E23: a refused replace leaves the live retrieval settings alone" "$topk" "7"
check_eq "E23: ... and the collection's chunk count" "$(count_of "$C")" "$chunks_before"
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":6,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
rm -f "$EXPORTS/$MIXGS"
(cd "$REPO_ROOT" && docker compose exec -T api rm -f \
  "/app/uploads/goldstandard_sessions/$MIX_SID.json" \
  "/app/uploads/goldstandard_sessions/gs_0000beef.json") >/dev/null 2>&1 || true

GOODGS=$(make_gs_pkg "$GS_SID" goodgs)
api_post "/import" "{\"filename\":\"$GOODGS\",\"on_conflict\":\"rename\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 1800 >/dev/null
api_get "/import/job/$ijob" > /tmp/vfy_gsjob.json
read -r gstat gname <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_gsjob.json')); print(d['status'], d['collection'])")"
check_eq "E23: a package with valid evaluation metadata still imports" "$gstat" "completed"
api_get "/goldstandard/session/$GS_SID" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if d['collection']=='$gname' and d['pairs'][0]['status']=='approved' else 1)"
check "E23: the valid session is restored against the imported collection" $? "session $GS_SID -> $gname"
rm -f "$EXPORTS/$GOODGS"
drop_collection "$gname"
# The restored session file outlives its collection by design (spec §8 rule 4).
(cd "$REPO_ROOT" && docker compose exec -T api \
  rm -f "/app/uploads/goldstandard_sessions/$GS_SID.json") >/dev/null 2>&1 || true

# ── conflict handling ────────────────────────────────────────────────────────
api_post "/import" "{\"filename\":\"$PKG\",\"on_conflict\":\"abort\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
code=$(api_get "/import/job/$ijob" | jfield "['error_code']")
check_eq "abort refuses an existing collection" "$code" "COLLECTION_EXISTS"

api_post "/import" "{\"filename\":\"$PKG\",\"on_conflict\":\"rename\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
istatus=$(wait_for_job "/import/job/$ijob" 1800)
api_get "/import/job/$ijob" > /tmp/vfy_impjob.json
read -r istat iname irenamed iwritten <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_impjob.json'))
print(d['status'], d['collection'], d['renamed'], d['chunks_written'])")"
check_eq "rename imports alongside the original" "$istat" "completed"
[ "$irenamed" = "True" ] && [ "$iname" != "$C" ]
check "the renamed collection has a new name" $? "imported as $iname"
check_eq "every chunk is imported" "$iwritten" "$chunks_before"

# ── import is lossless ───────────────────────────────────────────────────────
api_post "/export" "{\"collection\":\"$iname\"}" > /tmp/vfy_exp2.json
ejob2=$(python3 -c "import json;print(json.load(open('/tmp/vfy_exp2.json'))['job_id'])")
wait_for_job "/export/job/$ejob2" 1800 >/dev/null
PKG2=$(api_get "/export/job/$ejob2" | jfield "['filename']")
python3 - "$EXPORTS/$PKG" "$EXPORTS/$PKG2" <<'ENDPY'
import json, sys, tarfile, tempfile, pathlib
def chunks(path):
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(path) as t:
            t.extractall(td)
        root = next(p for p in pathlib.Path(td).iterdir() if p.is_dir())
        return {r["id"]: r for r in
                (json.loads(l) for l in (root / "chunks.jsonl").read_text().splitlines() if l.strip())}
a, b = chunks(sys.argv[1]), chunks(sys.argv[2])
same = set(a) == set(b) and all(a[k]["vector"] == b[k]["vector"]
                                and a[k]["properties"] == b[k]["properties"] for k in a)
sys.exit(0 if same else 1)
ENDPY
check "re-export after import is byte-identical (uuids, vectors, properties)" $?
rm -f "$EXPORTS/$PKG2"
drop_collection "$iname"

# ── models on import: damaged and namespaced (E26, E27) ──────────────────────
# Runs the importer inside the API process with its settings patched. E26 copies
# the embedding model into a temporary store and damages the copy; the live
# model store is only read. E27 uses the live store and a namespaced LLM name.
(cd "$REPO_ROOT" && docker compose exec -T api python - "$PKG") > /tmp/vfy_models.json 2>/tmp/vfy_models.err <<'ENDPY'
import json, shutil, sys, tempfile, uuid
from pathlib import Path
from unittest.mock import patch
from config import settings
from services import importer, model_bundle as models
pkg, out = sys.argv[1], {}

def run(conflict):
    jid = "vfy" + uuid.uuid4().hex[:6]
    importer._jobs[jid] = {"job_id": jid, "status": "queued", "filename": pkg,
                           "on_conflict": conflict, "collection": None,
                           "original_collection": None, "chunks_written": 0,
                           "fidelity": None, "renamed": False, "notes": [],
                           "error": None, "error_code": None, "error_detail": None}
    importer._active.add(pkg)
    importer._run(jid, pkg, conflict)
    return importer._jobs.pop(jid)

model = settings.embed_model
name = models.split_ref(model)[0]
out["live_intact"] = models.is_installed(model)
live_manifest = models.manifest_path(model)
digests = models._digests(json.loads(live_manifest.read_text()))
live_blobs = {d: models.blob_path(d) for d in digests}
live_stamps = {p: p.stat().st_mtime_ns for p in [live_manifest, *live_blobs.values()]}

# E26: installed but damaged -> MODEL_INTEGRITY_FAILED, the copy left as it was
with tempfile.TemporaryDirectory(prefix="vfy-model-", dir=settings.upload_dir) as td:
    with patch.object(settings, "ollama_models_dir", str(Path(td) / "store")):
        mp = models.manifest_path(model)
        mp.parent.mkdir(parents=True)
        shutil.copyfile(live_manifest, mp)
        for d, src in live_blobs.items():
            models.blob_path(d).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, models.blob_path(d))
        out["copy_present"] = models.is_installed(model)
        smallest = min(digests, key=lambda d: live_blobs[d].stat().st_size)
        damaged = models.blob_path(smallest)
        damaged.write_bytes(b"ordinary damaged review bytes")
        manifest_before = mp.read_bytes()
        job = run("rename")
        out["e26_status"] = job["status"]
        out["e26_code"] = job["error_code"]
        out["e26_message"] = job["error"] or ""
        out["e26_untouched"] = (damaged.read_bytes() == b"ordinary damaged review bytes"
                                and mp.read_bytes() == manifest_before)

# E27: a namespaced LLM that Ollama doesn't report -> import completes with a note
ns = "vfy-namespace/absent-llm"
reports = getattr(importer, "_ollama_reports", None)
if reports:                                              # real /api/tags; tag defaults to latest
    out["tags_have_embed"], out["ns_reported"] = reports(name), reports(ns)
with patch.object(settings, "llm_model", ns):
    job = run("rename")
out["e27_status"] = job["status"]
out["e27_error"] = f"{job['error_code']}: {job['error']}"
out["e27_collection"] = job["collection"]
out["e27_note"] = any(ns in n for n in job["notes"])
out["live_untouched"] = all(p.stat().st_mtime_ns == s for p, s in live_stamps.items())
print(json.dumps(out))
ENDPY
m() { python3 -c "import json,sys;print(json.load(open('/tmp/vfy_models.json'))[sys.argv[1]])" "$1" 2>/dev/null; }
check_eq "the live embedding model's files match their checksums" "$(m live_intact)" "True"
check_eq "the temporary copy of the embedding model starts intact" "$(m copy_present)" "True"
check_eq "E26 a damaged installed embedding model fails the import" "$(m e26_status)" "failed"
check_eq "E26 ... as MODEL_INTEGRITY_FAILED, not EMBEDDING_MODEL_MISSING" "$(m e26_code)" "MODEL_INTEGRITY_FAILED"
m e26_message | grep -qi "re-pull"
check "E26 the message says to restore or re-pull the model" $? "$(m e26_message)"
check_eq "E26 the damaged model's files are left untouched" "$(m e26_untouched)" "True"
check_eq "Ollama's /api/tags reports the embedding model by name (tag defaults to latest)" "$(m tags_have_embed)" "True"
check_eq "a namespaced model Ollama doesn't have is not reported" "$(m ns_reported)" "False"
check_eq "E27 with a namespaced LLM_MODEL, an import without bundled models completes" "$(m e27_status)" "completed"
[ "$(m e27_status)" = completed ] || printf '      %s\n' "$(m e27_error)"
check_eq "E27 the import notes the namespaced model" "$(m e27_note)" "True"
check_eq "the live model store was only read" "$(m live_untouched)" "True"
[ -s /tmp/vfy_models.json ] || sed 's/^/      /' /tmp/vfy_models.err | tail -5
e27c=$(m e27_collection); [ -n "$e27c" ] && [ "$e27c" != "None" ] && [ "$e27c" != "$C" ] && drop_collection "$e27c"

# ── tuning ───────────────────────────────────────────────────────────────────
api_get "/tune/$C" > /tmp/vfy_tune.json
check_eq "tune options report with-sources" "$(jfield "['fidelity']" < /tmp/vfy_tune.json)" "with-sources"
check_eq "re-chunking is offered" "$(jfield "['can_rechunk']" < /tmp/vfy_tune.json)" "True"

api_post "/tune/reindex" "{\"collection\":\"$C\",\"index_type\":\"flat\",\"distance_metric\":\"cosine\"}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
tstatus=$(wait_for_job "/tune/job/$tjob" 1800)
check_eq "re-index completes" "$tstatus" "completed"
api_get "/tune/job/$tjob" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if any('unchanged' in n for n in d['notes']) else 1)"
check "re-index leaves gold-standard sessions alone" $?
after_index=$(api_get "/collections" | python3 -c "
import json,sys; print([c['index_type'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")
check_eq "the index type actually changed" "$after_index" "flat"

api_post "/tune/rechunk" "{\"collection\":\"$C\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80,\"min_chunk_size\":30}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
tstatus=$(wait_for_job "/tune/job/$tjob" 1800)
check_eq "re-chunk completes" "$tstatus" "completed"
after_chunks=$(api_get "/tune/job/$tjob" | jfield "['chunks_written']")
[ "$after_chunks" -gt "$chunks_before" ]
check "re-chunking with a smaller size yields more chunks" $? "$chunks_before -> $after_chunks"

# ── chunks-only refuses re-chunking ──────────────────────────────────────────
SRCLESS="${PREFIX}Chunksonly"
drop_collection "$SRCLESS"; make_collection "$SRCLESS"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$SRCLESS" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_s.json
sjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_s.json'))['job_id'])")
wait_for_job "/ingest/job/$sjob" 900 >/dev/null
(cd "$REPO_ROOT" && docker compose exec -T api sh -c "rm -rf /app/sources/$SRCLESS") >/dev/null 2>&1
check_eq "a source-less collection reports chunks-only" \
  "$(api_get "/tune/$SRCLESS" | jfield "['fidelity']")" "chunks-only"
api_post "/tune/rechunk" "{\"collection\":\"$SRCLESS\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
wait_for_job "/tune/job/$tjob" 900 >/dev/null
check_eq "re-chunking a chunks-only collection is refused" \
  "$(api_get "/tune/job/$tjob" | jfield "['error_code']")" "SOURCES_REQUIRED"
# Use valid fixed settings so this tests source eligibility, not overlap validation.
api_post "/tune/reembed" "{\"collection\":\"$SRCLESS\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
wait_for_job "/tune/job/$tjob" 900 >/dev/null
check_eq "re-embedding a chunks-only collection with new chunking is refused" \
  "$(api_get "/tune/job/$tjob" | jfield "['error_code']")" "SOURCES_REQUIRED"
drop_collection "$SRCLESS"

# ── the help page and the package README share a source ──────────────────────
api_get "/help/transfer" > /tmp/vfy_help.json
python3 - "$EXPORTS/$PKG" <<'ENDPY'
import json, pathlib, sys, tarfile, tempfile
help_md = json.load(open('/tmp/vfy_help.json'))['markdown']
partials = pathlib.Path('../../api/templates/partials')
with tempfile.TemporaryDirectory() as td:
    with tarfile.open(sys.argv[1]) as t:
        t.extractall(td)
    root = next(p for p in pathlib.Path(td).iterdir() if p.is_dir())
    readme = (root / "README.md").read_text()
missing = []
for part in sorted(partials.glob("*.md")):
    probe = max(part.read_text().split("\n"), key=len).strip()
    if "@@" in probe:
        probe = probe.split("@@")[0].strip()
    if not probe:
        continue
    if probe not in help_md:
        missing.append(f"{part.stem}: absent from help page")
    # retrieve_usage only appears in a README when that package ships a script
    elif probe not in readme and part.stem != "retrieve_usage":
        missing.append(f"{part.stem}: absent from package README")
sys.exit(0 if not missing else 1)
ENDPY
check "help page and package README render from the same partials" $?
python3 -c "
import json,re,sys
m=json.load(open('/tmp/vfy_help.json'))['markdown']
sys.exit(0 if not re.search(r'@@[A-Z_0-9]+@@', m) else 1)"
check "the help page has no unsubstituted placeholders" $?

rm -f "$EXPORTS/$PKG"
drop_collection "$C"
cleanup_prefixed
bash ./10_validity.sh
check "retained-session validity acceptance suite" $?
summary
