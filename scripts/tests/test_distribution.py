"""The distribution scripts build the archives SPECIFICATIONS.md section 11 describes (issue #387).

`package.sh` and `package-offline.sh` run in a copy of the tracked tree with files planted
that section 11.2 says no archive may carry; the archives they write are then read back.
`package-offline.sh` and `install-offline.sh` run with `docker` and `sleep` replaced by stubs
on PATH that log every call, so nothing is saved, loaded or started and no stack is touched.
`tar`, `gzip` and `zip` are real. The image list is compared with what `docker compose config` resolves for
`docker-compose.yml`, which only reads the file. `scripts/verify/transfer_packages.py`, suite 15's
package damager, is run on small packages of its own: one with an absolute name, a `..`, a
symbolic or a hard link is refused with nothing written, and a safe one is damaged (#490).

Runs on the host with the standard library (Python 3.9+), from the repository root:

    python3 scripts/tests/test_distribution.py

`package.sh` needs `zip` and `git`, and the image check needs `docker compose`; each test is
skipped without them. `15_transfer_paths.sh` runs it.
"""
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# What section 11.2 and the scripts exclude, planted where the project has such things.
PLANTED = {
    ".DS_Store": "finder", "ui/.DS_Store": "finder",
    "ui/node_modules/react/index.js": "module", "api/__pycache__/main.cpython-312.pyc": "bytecode",
    "api/stray.pyc": "bytecode", "ui/dist/index.html": "built", "api/.venv/bin/python": "venv",
    "api/venv/bin/python": "venv", ".env": "SECRET=1", "api/.env": "SECRET=1",
    "ui/.env.local": "SECRET=1",
    "ingest-inbox/report.txt": "corpus", "exports/ragpkg-corpus.tar.gz": "corpus",
}
PLANTED_ZIP = {"api/uploads/upload.txt": "upload", "old-build.zip": "archive"}
PLANTED_TAR = {"old-bundle.tar": "archive", "offline/stale-images.tar.gz": "old"}
# section 11.2 excludes `.env*`; a top-level `.env.<suffix>` is checked on its own (see below).
# Planted files are untracked and gitignored, as in a real checkout: `git archive` must leave them out.
# Git ignores the user's and the system's configuration, and takes its identity from the environment.
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
# The commit runs with automatic maintenance off (#696). Otherwise `git commit` starts a detached
# `git maintenance run --auto`, whose repack (git 2.54) can still be writing into `.git` when the
# temporary directory is removed, and the cleanup fails with "Directory not empty".
# `gc.auto=0` does not stop it.
NO_AUTO_MAINTENANCE = ["-c", "maintenance.auto=false"]
TOP_LEVEL_ENV = ".env.local"

STUB_DOCKER = r"""#!/bin/bash
# Logs each call, one line, each argument quoted, and answers as the test configures.
printf '%q ' "$@" >> "$STUB_LOG"; echo >> "$STUB_LOG"
case "$1 $2" in
  "image inspect")
    for missing in $STUB_MISSING_IMAGES; do [ "$3" = "$missing" ] && exit 1; done; exit 0 ;;
  "volume inspect") [ -n "$STUB_NO_VOLUME" ] && exit 1; exit 0 ;;
  # The ollama service's LLM settings (#671): the defaults docker-compose.yml sets,
  # unless the test gives others.
  "compose config")
    printf '{"name": "rag-docker", "services": {"ollama": {"environment": {"LLM_MODEL": "%s", "BUILD_LLM_MODEL": "%s", "ANSWER_LLM_MODEL": "%s"}}}}\n' \
      "${STUB_LLM_MODEL-llama3.1:8b}" "${STUB_BUILD_LLM_MODEL:-}" "${STUB_ANSWER_LLM_MODEL:-}"
    exit 0 ;;
  "compose ps")
    if [ "$3" = "--services" ]; then
      i=0; while [ "$i" -lt "${STUB_RUNNING:-5}" ]; do echo "service$i"; i=$((i+1)); done
    else
      echo "NAME STATUS"
    fi
    exit 0 ;;
  "compose run"|"compose up"|"load -i") exit 0 ;;
esac
case "$1" in
  info) exit "${STUB_INFO_RC:-0}" ;;
  build) exit "${STUB_BUILD_RC:-0}" ;;
  save) shift; printf 'saved: %s' "$*"; exit 0 ;;
  run)
    for arg in "$@"; do
      case "$arg" in *:/out)
        out="${arg%:/out}"; mkdir -p "$STUB_MODELS/models"; echo weights > "$STUB_MODELS/models/blob"
        tar czf "$out/ollama_models.tar.gz" -C "$STUB_MODELS" .
        exit 0 ;;
      esac
    done
    # The model check: it prints the models whose manifest is missing.
    for m in $STUB_MISSING_MODELS; do echo "$m"; done
    exit "${STUB_CHECK_RC:-0}" ;;
esac
echo "stub docker: unexpected call: $*" >&2
exit 99
"""
STUB_SLEEP = """#!/bin/bash
printf 'sleep %s\\n' "$*" >> "$STUB_LOG"
"""


def offline_images():
    """The IMAGES array of package-offline.sh, as written."""
    text = (ROOT / "package-offline.sh").read_text()
    block = re.search(r"^IMAGES=\(\n(.*?)^\)", text, re.S | re.M).group(1)
    return [line.strip() for line in block.splitlines() if line.strip()]


def ollama_image():
    """The one ollama/ollama image of package-offline.sh's IMAGES list (#490)."""
    found = [image for image in offline_images() if image.startswith("ollama/ollama:")]
    assert len(found) == 1, found
    return found[0]


def age_image():
    """The tag scripts/backup-common.sh gives the age image (#852, #856): the age version and
    the first 12 hex digits of the SHA-256 of scripts/age/Dockerfile's instruction lines
    (comments and blank lines left out)."""
    text = (ROOT / "scripts" / "age" / "Dockerfile").read_text()
    version = re.search(r"^RUN apk add --no-cache age=(\S+)$", text, re.M).group(1)
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    digest = hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()
    return f"rag-docker-age:{version}-{digest[:12]}"


def entrypoint_models(**settings):
    """The models ollama/entrypoint.sh pulls with these LLM settings (#671): what
    ollama/models.sh prints, the names package-offline.sh and the CI cache read."""
    env = {"PATH": os.environ["PATH"], "LLM_MODEL": "llama3.1:8b", **settings}
    out = subprocess.run(["bash", str(ROOT / "ollama" / "models.sh")], capture_output=True,
                         text=True, env=env, timeout=60, check=True)
    return out.stdout.split()


def background_git(trace):
    """The `git maintenance` and `git gc` commands a trace2 event file shows git starting (#696)."""
    if not trace.exists():
        return []
    started = []
    for line in trace.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        argv = event.get("argv") or []
        if event.get("event") == "child_start" and argv[1:2] in (["maintenance"], ["gc"]):
            started.append(" ".join(argv))
    return started


def tracked_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        return None
    return [name for name in out.stdout.decode().split("\0") if name]


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.log = self.temp / "docker.log"
        self.log.touch()
        stub = self.temp / "bin"
        stub.mkdir()
        for name, text in (("docker", STUB_DOCKER), ("sleep", STUB_SLEEP)):
            (stub / name).write_text(text)
            (stub / name).chmod(0o755)
        self.env = dict(os.environ, **GIT_ENV, PATH=f"{stub}:{os.environ['PATH']}",
                        STUB_LOG=str(self.log), STUB_MODELS=str(self.temp / "models"))

    def calls(self):
        return [shlex.split(line) for line in self.log.read_text().splitlines() if line.strip()]

    def source_tree(self, planted):
        files = tracked_files()
        if files is None:
            self.skipTest("needs a git checkout")
        tree = self.temp / "src"
        tree.mkdir()
        for name in files:
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, target)
        # package.sh and package-offline.sh run `git archive HEAD`: make the copy a repository
        # with the tracked files committed, then plant the excluded files (untracked) on top.
        # The commit is traced, so the check below sees any background git work it starts (#696).
        git = dict(os.environ, **GIT_ENV)
        trace = self.temp / "git-commit-trace.json"
        listed = "\0".join(name for name in files if (ROOT / name).is_file()) + "\0"
        for args, data, extra in ((["init", "-q", "--template="], None, {}),
                                  (["add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
                                   listed, {}),
                                  ([*NO_AUTO_MAINTENANCE, "commit", "-q", "-m", "tracked files"],
                                   None, {"GIT_TRACE2_EVENT": str(trace)})):
            done = subprocess.run(["git", "-C", str(tree), *args], input=data, text=True,
                                  capture_output=True, env=dict(git, **extra))
            self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(background_git(trace), [], "git commit started background git work, "
                         "which races the temporary directory's removal (#696)")
        for name, text in planted.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text(text)
        return tree, [name for name in files if (ROOT / name).is_file()]

    def run_script(self, script, *args, cwd=None, **env):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                              cwd=cwd or self.temp, env=dict(self.env, **env), timeout=600)

    def assert_contents(self, names, files, prefix, planted):
        names = set(names)
        missing = [name for name in files if prefix + name not in names]
        self.assertEqual(missing, [], "tracked files missing from the archive")
        leaked = [name for name in planted if prefix + name in names]
        self.assertEqual(leaked, [], "excluded files in the archive")
        for keep in ("ingest-inbox/.gitkeep", "exports/.gitkeep"):
            self.assertIn(prefix + keep, names)
        for folder in ("ingest-inbox/", "exports/"):
            inside = sorted(n for n in names if n.startswith(prefix + folder)
                            and n.rstrip("/") != prefix + folder.rstrip("/"))
            self.assertEqual(inside, [prefix + folder + ".gitkeep"])


@unittest.skipUnless(shutil.which("zip"), "needs zip")
class PackageTests(Workspace):
    def build(self, planted):
        tree, files = self.source_tree(planted)
        out = self.temp / "out" / "rag-docker.zip"
        out.parent.mkdir()
        result = self.run_script(tree / "package.sh", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Created {out}", result.stdout)
        with zipfile.ZipFile(out) as archive:
            return archive.namelist(), files

    def test_the_zip_holds_the_tracked_tree_and_nothing_excluded(self):
        planted = dict(PLANTED, **PLANTED_ZIP)
        names, files = self.build(planted)
        self.assert_contents(names, files, "", planted)

    def test_a_top_level_env_file_is_excluded(self):
        names, _files = self.build({TOP_LEVEL_ENV: "SECRET=1"})
        self.assertNotIn(TOP_LEVEL_ENV, names)


class OfflinePackageTests(Workspace):
    def build(self, planted, **env):
        tree, files = self.source_tree(planted)
        out = self.temp / "out" / "bundle.tar"
        out.parent.mkdir(exist_ok=True)
        result = self.run_script(tree / "package-offline.sh", str(out), cwd=tree, **env)
        return result, out, files

    def test_the_bundle_holds_the_source_images_and_weights(self):
        planted = dict(PLANTED, **PLANTED_TAR)
        result, out, files = self.build(planted)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(out) as bundle:
            names = [m.name.rstrip("/") + ("/" if m.isdir() else "") for m in bundle.getmembers()]
            names = [n[2:] if n.startswith("./") else n for n in names]
            self.assertTrue(all(n == "rag-docker/" or n.startswith("rag-docker/") for n in names),
                            names[:5])
            self.assert_contents(names, files, "rag-docker/", planted)
            self.assertIn("rag-docker/install-offline.sh", names)
            images = gzip.decompress(bundle.extractfile("rag-docker/offline/images.tar.gz").read())
            weights = bundle.extractfile("rag-docker/offline/ollama_models.tar.gz").read()
            offline = sorted(n for n in names if n.startswith("rag-docker/offline/"))
        # The compose images and the age image the backup scripts run in (#852).
        self.assertEqual(images.decode(), "saved: " + " ".join(offline_images() + [age_image()]))
        with tarfile.open(fileobj=io.BytesIO(weights), mode="r:gz") as models:
            self.assertIn("./models/blob", models.getnames())
        self.assertEqual(offline, ["rag-docker/offline/", "rag-docker/offline/images.tar.gz",
                                   "rag-docker/offline/ollama_models.tar.gz"])
        calls = self.calls()
        self.assertEqual([c[2] for c in calls if c[:2] == ["image", "inspect"]],
                         offline_images() + [age_image()])
        self.assertIn(["volume", "inspect", "rag-docker_ollama_models"], calls)
        save = [c for c in calls if c[0] == "save"]
        self.assertEqual(save, [["save"] + offline_images() + [age_image()]])
        self.assertEqual([c for c in calls if c[0] == "build"], [])
        run = next(c for c in calls if c[0] == "run")
        self.assertIn("rag-docker_ollama_models:/models:ro", run)
        self.assertIn(ollama_image(), run)
        # The export runs the listed image too (#490).
        export = next(c for c in calls if c[0] == "run" and any(a.endswith(":/out") for a in c))
        self.assertIn(ollama_image(), export)

    def test_a_top_level_env_file_is_excluded(self):
        result, out, _files = self.build({TOP_LEVEL_ENV: "SECRET=1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(out) as bundle:
            names = bundle.getnames()
        self.assertNotIn("rag-docker/" + TOP_LEVEL_ENV, names)

    def test_a_missing_image_stops_before_anything_is_saved(self):
        result, out, _files = self.build({}, STUB_MISSING_IMAGES="nginx:1.29-alpine")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: image 'nginx:1.29-alpine' not found locally.", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual([c for c in self.calls() if c[0] in ("save", "run")], [])

    def test_a_missing_age_image_is_built_from_the_bundles_source(self):
        # Built from the staged copy of scripts/age, the one the bundle ships (#852).
        result, out, _files = self.build({}, STUB_MISSING_IMAGES=age_image())
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [c for c in self.calls() if c[0] == "build"]
        self.assertEqual(len(builds), 1, builds)
        self.assertEqual(builds[0][:4], ["build", "-q", "-t", age_image()])
        context = Path(builds[0][4])
        self.assertEqual(context.parts[-3:], ("rag-docker", "scripts", "age"))
        self.assertNotEqual(context, (self.temp / "src" / "scripts" / "age").resolve())
        self.assertIn(f"Building the age image {age_image()}", result.stdout)
        save = [c for c in self.calls() if c[0] == "save"]
        self.assertEqual(save, [["save"] + offline_images() + [age_image()]])
        self.assertTrue(out.exists())

    def test_a_failed_age_image_build_stops_before_anything_is_saved(self):
        result, out, _files = self.build({}, STUB_MISSING_IMAGES=age_image(), STUB_BUILD_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"could not build the age image {age_image()}", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual([c for c in self.calls() if c[0] == "save"], [])

    def test_a_missing_model_volume_stops_before_anything_is_saved(self):
        result, out, _files = self.build({}, STUB_NO_VOLUME="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: volume 'rag-docker_ollama_models' not found.", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual([c for c in self.calls() if c[0] in ("save", "run")], [])


    def test_the_model_check_reads_the_entrypoint_names_from_the_volume_read_only(self):
        result, _out, _files = self.build({})
        self.assertEqual(result.returncode, 0, result.stderr)
        models = entrypoint_models()
        self.assertIn("llama3.1:8b", models)
        runs = [c for c in self.calls() if c[0] == "run"]
        self.assertEqual(len(runs), 2)
        check, export = runs
        self.assertEqual(check[-len(models) - 1:], ["sh"] + models)
        for arg in ("--network", "none", "rag-docker_ollama_models:/models:ro", ollama_image()):
            self.assertIn(arg, check)
        self.assertFalse(any(a.endswith(":/out") for a in check))
        self.assertTrue(any(a.endswith(":/out") for a in export))
        # The check comes before anything is saved.
        calls = self.calls()
        self.assertLess(calls.index(check), next(i for i, c in enumerate(calls) if c[0] == "save"))

    def test_a_different_build_model_is_checked_too(self):
        # The names come from the LLM settings compose gives the ollama service (#671).
        result, _out, _files = self.build({}, STUB_BUILD_LLM_MODEL="qwen2.5:0.5b")
        self.assertEqual(result.returncode, 0, result.stderr)
        models = entrypoint_models(BUILD_LLM_MODEL="qwen2.5:0.5b")
        self.assertEqual(models, ["llama3.1:8b", "qwen2.5:0.5b", "nomic-embed-text"])
        check = [c for c in self.calls() if c[0] == "run"][0]
        self.assertEqual(check[-len(models) - 1:], ["sh"] + models)

    def test_the_model_check_finds_library_and_namespaced_models(self):
        # The check's own script, run against a model store with one library and one
        # namespaced model (#671): a namespaced name isn't under library/.
        result, _out, _files = self.build({}, STUB_BUILD_LLM_MODEL="someone/assistant")
        self.assertEqual(result.returncode, 0, result.stderr)
        check = [c for c in self.calls() if c[0] == "run"][0]
        # The script as package-offline.sh writes it (the stub's log quotes newlines).
        text = (ROOT / "package-offline.sh").read_text()
        script = re.search(r"-c '(for m in .*?done)' sh \$MODELS", text, re.S).group(1)
        store = self.temp / "store"
        manifests = store / "models" / "manifests" / "registry.ollama.ai"
        for path in ("library/llama3.1/8b", "library/nomic-embed-text/latest",
                     "someone/assistant/latest"):
            (manifests / path).parent.mkdir(parents=True, exist_ok=True)
            (manifests / path).write_text("{}")
        names = ["llama3.1:8b", "someone/assistant", "nomic-embed-text"]
        self.assertEqual(check[-len(names):], names)
        run = lambda *models: subprocess.run(
            ["sh", "-c", script.replace("/models/", f"{store}/"), "sh", *models],
            capture_output=True, text=True, timeout=60)
        found = run(*names)
        self.assertEqual((found.returncode, found.stdout), (0, ""), found.stderr)
        missing = run("someone/other", "library-less:1", "llama3.1:8b")
        self.assertEqual(missing.stdout.split(), ["someone/other", "library-less:1"])

    def test_the_model_check_looks_under_a_models_own_host(self):
        # A name with three segments starts with a registry host (#772, #824): its manifest is
        # under that host's directory, not under registry.ollama.ai.
        text = (ROOT / "package-offline.sh").read_text()
        script = re.search(r"-c '(for m in .*?done)' sh \$MODELS", text, re.S).group(1)
        store = self.temp / "store"
        manifests = store / "models" / "manifests"
        for path in ("hf.co/user/model/latest", "hf.co/user/model/Q4_K_M",
                     "registry.ollama.ai/library/llama3.1/8b",
                     "registry.ollama.ai/someone/assistant/latest",
                     "registry.ollama.ai/hf.co/other/model/latest"):
            (manifests / path).parent.mkdir(parents=True, exist_ok=True)
            (manifests / path).write_text("{}")
        run = lambda *models: subprocess.run(
            ["sh", "-c", script.replace("/models/", f"{store}/"), "sh", *models],
            capture_output=True, text=True, timeout=60)
        found = run("hf.co/user/model", "hf.co/user/model:Q4_K_M", "llama3.1:8b", "someone/assistant")
        self.assertEqual((found.returncode, found.stdout), (0, ""), found.stderr)
        # A host model that is only under registry.ollama.ai, or not stored, is missing; a
        # library name with a dot (llama3.1:8b) is not a host.
        missing = run("hf.co/other/model", "hf.co/user/absent", "hf.co/user/model:nope",
                      "someone/assistant:v2", "llama3.1:9b")
        self.assertEqual(missing.stdout.split(), ["hf.co/other/model", "hf.co/user/absent",
                                                  "hf.co/user/model:nope", "someone/assistant:v2",
                                                  "llama3.1:9b"])

    def test_the_model_check_follows_ollamas_name_parse(self):
        # Ollama parses a name from the right (#824): the tag is after the last ":" of the last
        # segment, so a port isn't a tag, and the first segment is a host only with three segments.
        text = (ROOT / "package-offline.sh").read_text()
        script = re.search(r"-c '(for m in .*?done)' sh \$MODELS", text, re.S).group(1)
        def make(name, *paths):
            manifests = self.temp / name / "models" / "manifests"
            for path in paths:
                (manifests / path).parent.mkdir(parents=True, exist_ok=True)
                (manifests / path).write_text("{}")
            return self.temp / name
        run = lambda store, *models: subprocess.run(
            ["sh", "-c", script.replace("/models/", f"{store}/"), "sh", *models],
            capture_output=True, text=True, timeout=60)
        store = make("store", "registry.ollama.ai/hf.co/model/latest",
                     "reg.example:5000/u/m/tag", "reg.example:5000/u/m/latest")
        found = run(store, "hf.co/model", "reg.example:5000/u/m:tag", "reg.example:5000/u/m")
        self.assertEqual((found.returncode, found.stdout), (0, ""), found.stderr)
        # Only the old, wrong places: hf.co as a host, and the port dropped.
        wrong = make("store2", "hf.co/model/latest", "reg.example/u/m/tag")
        names = ["hf.co/model", "reg.example:5000/u/m:tag", "reg.example:5000/u/m", "a/b/c/d"]
        missing = run(wrong, *names)
        self.assertEqual((missing.returncode, missing.stdout.split()), (0, names), missing.stderr)

    def test_a_host_model_name_reaches_the_check_unchanged(self):
        result, _out, _files = self.build({}, STUB_BUILD_LLM_MODEL="hf.co/user/model")
        self.assertEqual(result.returncode, 0, result.stderr)
        check = [c for c in self.calls() if c[0] == "run"][0]
        self.assertEqual(check[-4:], ["sh", "llama3.1:8b", "hf.co/user/model", "nomic-embed-text"])

    def test_unreadable_model_settings_stop_before_anything_is_saved(self):
        # Spaces, and the loose names #772 refuses: "..", a leading "/" and a leading "-".
        for bad in ("bad name", "a..b", "../x", "/x", "-x"):
            with self.subTest(name=bad):
                # source_tree makes "src" with mkdir(), so each case starts without it.
                shutil.rmtree(self.temp / "src", ignore_errors=True)
                result, out, _files = self.build({}, STUB_BUILD_LLM_MODEL=bad)
                self.assertEqual(result.returncode, 1)
                self.assertIn("BUILD_LLM_MODEL is not a model name", result.stderr)
                self.assertIn("ERROR: could not read the model names (ollama/compose-models.sh).",
                              result.stderr)
                self.assertFalse(out.exists())
                self.assertEqual([c for c in self.calls() if c[0] in ("save", "run")], [])

    def test_a_missing_model_stops_before_anything_is_saved(self):
        # An install from before #647 holds phi3.5, not llama3.1:8b.
        result, out, _files = self.build({}, STUB_MISSING_MODELS="llama3.1:8b")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: volume 'rag-docker_ollama_models' doesn't hold llama3.1:8b. "
                      "Start the stack once (docker compose up -d)", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual([c for c in self.calls() if c[0] == "save"], [])
        self.assertEqual(len([c for c in self.calls() if c[0] == "run"]), 1)

    def test_a_failed_model_check_stops_before_anything_is_saved(self):
        result, out, _files = self.build({}, STUB_CHECK_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: could not read the models in volume 'rag-docker_ollama_models'.",
                      result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual([c for c in self.calls() if c[0] == "save"], [])


class ImageListTests(unittest.TestCase):
    def test_the_offline_images_are_the_images_compose_resolves(self):
        if not shutil.which("docker"):
            self.skipTest("needs the docker CLI")
        env = {k: v for k, v in os.environ.items()
               if k not in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES")}
        if subprocess.run(["docker", "compose", "version"], capture_output=True,
                          env=env).returncode != 0:
            self.skipTest("needs docker compose")
        out = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"),
                              "--project-directory", str(ROOT), "config", "--images"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        resolved = sorted(line.strip() for line in out.stdout.splitlines() if line.strip())
        self.assertEqual(sorted(offline_images()), resolved)


class ComposeModelsTests(unittest.TestCase):
    def test_the_default_settings_resolve_to_todays_models(self):
        # docker-compose.yml's own settings, read as package-offline.sh and the CI
        # model cache read them (#671): unchanged from before the split.
        if not shutil.which("docker"):
            self.skipTest("needs the docker CLI")
        env = {k: v for k, v in os.environ.items()
               if k not in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES")}
        if subprocess.run(["docker", "compose", "version"], capture_output=True,
                          env=env).returncode != 0:
            self.skipTest("needs docker compose")
        with tempfile.TemporaryDirectory() as folder:
            # Only the tracked compose file, so a local docker-compose.override.yml
            # can't change what is read.
            for name in ("docker-compose.yml", "ollama/models.sh", "ollama/compose-models.sh"):
                target = Path(folder) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            out = subprocess.run(["bash", "ollama/compose-models.sh"], capture_output=True,
                                 text=True, cwd=folder, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["llama3.1:8b", "nomic-embed-text"])
        self.assertEqual(out.stdout.split(), entrypoint_models())


class InstallOfflineTests(Workspace):
    def bundle(self, *offline):
        folder = self.temp / "bundle"
        (folder / "offline").mkdir(parents=True)
        shutil.copy2(ROOT / "install-offline.sh", folder)
        for name in offline:
            (folder / "offline" / name).write_bytes(b"stub")
        return folder

    def install(self, folder, **env):
        return self.run_script(folder / "install-offline.sh", cwd=self.temp, **env)

    def test_without_the_images_it_refuses_naming_them(self):
        result = self.install(self.bundle("ollama_models.tar.gz"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: offline/images.tar.gz missing.", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_the_weights_it_refuses_naming_them(self):
        result = self.install(self.bundle("images.tar.gz"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: offline/ollama_models.tar.gz missing.", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_docker_running_it_refuses(self):
        result = self.install(self.bundle("images.tar.gz", "ollama_models.tar.gz"), STUB_INFO_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: Docker is not running.", result.stderr)
        self.assertEqual(self.calls(), [["info"]])

    def test_it_loads_restores_and_starts_without_building(self):
        folder = self.bundle("images.tar.gz", "ollama_models.tar.gz")
        result = self.install(folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            ["info"],
            ["load", "-i", "offline/images.tar.gz"],
            ["compose", "run", "--rm", "--no-deps", "--entrypoint", "sh",
             "-v", f"{folder}/offline:/backup:ro", "ollama", "-c",
             "tar xzf /backup/ollama_models.tar.gz -C /root/.ollama && ls /root/.ollama"],
            ["compose", "up", "-d", "--no-build"],
            ["compose", "ps", "--services", "--filter", "status=running"],
            ["compose", "ps"],
        ])

    def test_its_wait_counts_running_services_and_fails_after_60_polls(self):
        # The wait counts running services, as it says: compose up has already waited for the
        # healthchecked ones. One still missing after 60 polls fails the install (#490).
        result = self.install(self.bundle("images.tar.gz", "ollama_models.tar.gz"), STUB_RUNNING="4")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        calls = self.calls()
        polls = [c for c in calls if c == ["compose", "ps", "--services", "--filter", "status=running"]]
        self.assertEqual(len(polls), 60)
        self.assertEqual(calls.count(["sleep", "5"]), 60)
        self.assertEqual(calls[-1], ["compose", "ps"])
        self.assertIn("Waiting for the 5 services to be running (docker compose up has already waited "
                      "for weaviate, ollama and api to report healthy)...", result.stdout)
        self.assertIn("ERROR: only 4 of 5 services are running after 5 minutes.", result.stderr)
        self.assertNotIn("http://localhost:8080", result.stdout)


class TransferPackagesTests(unittest.TestCase):
    """scripts/verify/transfer_packages.py checks every member before extracting (#490)."""
    STEM = "ragpkg-test-490"

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="rd490-")).resolve()
        self.addCleanup(shutil.rmtree, self.temp, True)

    def package(self, extra=None):
        path = self.temp / (self.STEM + ".tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            for name, data in (("", None), ("manifest.json", b'{"files": {}}'), ("chunks.jsonl", b"{}\n")):
                info = tarfile.TarInfo(self.STEM + ("/" + name if name else ""))
                if data is None:
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                else:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            if extra is not None:
                data = b"" if extra.type in (tarfile.SYMTYPE, tarfile.LNKTYPE) else b"outside\n"
                extra.size = len(data)
                tar.addfile(extra, io.BytesIO(data))
        return path

    def damage(self, path):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "verify" / "transfer_packages.py"),
                               str(path), "badmanifest"], capture_output=True, text=True, timeout=60)

    def test_a_package_with_an_unsafe_member_is_refused(self):
        def link(name, kind, target):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, target
            return info
        cases = {
            "an absolute name": tarfile.TarInfo("/tmp/abs-490.txt"),
            "a '..' in its name": tarfile.TarInfo(self.STEM + "/../escape-490.txt"),
            "not a regular file or a directory (symbolic link)":
                link(self.STEM + "/link", tarfile.SYMTYPE, "manifest.json"),
            "not a regular file or a directory (hard link)":
                link(self.STEM + "/hard", tarfile.LNKTYPE, self.STEM + "/manifest.json"),
        }
        for case, member in cases.items():
            with self.subTest(case):
                result = self.damage(self.package(member))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"refused member '{member.name}': {case.split(' (')[0]}; nothing was written",
                              result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(sorted(p.name for p in self.temp.iterdir()), [self.STEM + ".tar.gz"])

    def test_a_safe_package_gives_its_damaged_copy(self):
        result = self.damage(self.package())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.STEM + "-badmanifest.tar.gz")
        with tarfile.open(self.temp / result.stdout.strip()) as tar:
            manifest = tar.extractfile(self.STEM + "/manifest.json").read()
        self.assertEqual(manifest, b"{not json")


if __name__ == "__main__":
    unittest.main()
