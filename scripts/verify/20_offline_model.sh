#!/usr/bin/env bash
# Hugging Face libraries run offline in the api container (#618): the sentence-transformers
# model is pre-cached in the image, so loading it must make no network call.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack
section "Offline model load (#618)"
(cd "$REPO_ROOT" && docker compose exec -T api python - <<'ENDPY'
import os, sys, time
import huggingface_hub.constants as hc
print("offline flags:", os.environ.get("HF_HUB_OFFLINE"), os.environ.get("TRANSFORMERS_OFFLINE"), hc.HF_HUB_OFFLINE)
if not (os.environ.get("HF_HUB_OFFLINE") == "1" and os.environ.get("TRANSFORMERS_OFFLINE") == "1" and hc.HF_HUB_OFFLINE):
    sys.exit(1)
os.environ["HF_ENDPOINT"] = "http://10.255.255.1:9"   # unroutable: any network call would hang
from sentence_transformers import SentenceTransformer
t = time.time()
SentenceTransformer("all-MiniLM-L6-v2")
print("model loaded in %.1fs" % (time.time() - t))
sys.exit(0 if time.time() - t < 60 else 1)
ENDPY
)
check "api loads the pre-cached sentence-transformers model offline, with no Hugging Face call (#618)" $?
# Unstructured's usage telemetry is off in the running api container (#695):
# compose sets DO_NOT_TRACK=1.
dnt="$(cd "$REPO_ROOT" && docker compose exec -T api printenv DO_NOT_TRACK)"
[ "$dnt" = "1" ]
check "DO_NOT_TRACK is 1 in the running api container (#695; got '$dnt')" $?
# Ingest never dials out (#695): real ingest jobs of every supported type
# (.txt, .md, .pdf, .docx, .csv, .json), with the backends faked, and a
# preparation of a .txt and a .md with a stub LLM (#640), in a fresh
# container from the api image with no network. Every name lookup and
# non-Unix connect is refused and fails the test.
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_offline_ingest.py)
check "an ingest of every supported type, and a preparation of a .txt and a .md, succeed in a fresh api container with no network, and nothing dials out (#695, #640)" $?
summary
