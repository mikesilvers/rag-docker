"""ollama/entrypoint.sh skips models already present, pulls with a timeout and retries (issue #399).

It pulls the models ollama/models.sh resolves from the LLM settings (#671): the answer model,
the build model when it is a different model, and the embedding model. `models.sh present`, the
ollama healthcheck, passes only when every one of them is listed.

Feature-review item PL:G8. Runs on the host with Python's standard library and bash
(`16_api_paths.sh`, run by `01_infrastructure.sh`):

    python3 scripts/tests/test_ollama_entrypoint.py

The real script runs with stub `ollama`, `timeout` and `sleep` commands first on PATH:
nothing is downloaded and nothing waits. The stubs log every call, so each test reads
back what the script ran.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "ollama" / "entrypoint.sh"
MODELS_SH = ROOT / "ollama" / "models.sh"
MODELS = ("llama3.1:8b", "nomic-embed-text")
LLM = MODELS[0]
SMALL = "qwen2.5:0.5b"

# `ollama`: `serve` exits at once; `list` fails while list-fail tokens remain, then
# prints a header and one line per pulled model; `pull <m>` fails while fail-<m>
# tokens remain, else records <m> as present.
OLLAMA = r"""#!/bin/bash
state="$STUB_STATE"
echo "ollama $*" >> "$state/calls"
take() {  # take <file>: consume one token from <file>, succeed if there was one
  local n; n=$(cat "$1" 2>/dev/null || echo 0)
  [ "$n" -gt 0 ] || return 1
  echo $((n - 1)) > "$1"
}
case "$1" in
  serve) exit 0 ;;
  list)
    take "$state/list-fail" && exit 1
    # Ollama's table writer separates the columns with tabs.
    printf 'NAME\tID\tSIZE\tMODIFIED\n'
    for m in "$state"/models/*; do
      [ -e "$m" ] || continue
      # As Ollama does: a name pulled without a tag is listed with ":latest".
      name=$(basename "$m"); case "$name" in *:*) ;; *) name="$name:latest" ;; esac
      printf '%s\t0123456789ab\t2.2 GB\t1 day ago\n' "$name"
    done
    exit 0 ;;
  pull)
    take "$state/fail-$2" && { echo "pull failed" >&2; exit 1; }
    touch "$state/models/$2"
    exit 0 ;;
esac
exit 2
"""

# `timeout <seconds> <command...>`: exits 124, as a timed-out command does, while
# hang-<model> tokens remain, else runs the command.
TIMEOUT = r"""#!/bin/bash
state="$STUB_STATE"
echo "timeout $*" >> "$state/calls"
n=$(cat "$state/hang-$4" 2>/dev/null || echo 0)
if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$state/hang-$4"; exit 124; fi
shift
exec "$@"
"""

SLEEP = r"""#!/bin/bash
echo "sleep $*" >> "$STUB_STATE/calls"
"""


class EntrypointTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp, True)
        self.bin = Path(temp) / "bin"
        self.state = Path(temp) / "state"
        (self.state / "models").mkdir(parents=True)
        self.bin.mkdir()
        for name, text in (("ollama", OLLAMA), ("timeout", TIMEOUT), ("sleep", SLEEP)):
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)

    def present(self, *models):
        for model in models:
            (self.state / "models" / model).touch()

    def tokens(self, name, count):
        (self.state / name).write_text(str(count))

    def run_entrypoint(self, **settings):
        return self.run_script(ENTRYPOINT, **settings)

    def run_script(self, script, *args, **settings):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "STUB_STATE": str(self.state),
               "HOME": str(self.state), **settings}
        result = subprocess.run(["bash", str(script), *args], env=env, capture_output=True,
                                text=True, timeout=60)
        calls_file = self.state / "calls"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
        return result, calls

    def pulls(self, calls):
        return [c for c in calls if c.startswith("timeout ")]


class EntrypointTests(EntrypointTestCase):
    def test_models_already_present_are_not_pulled(self):
        self.present(*MODELS)
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [])
        self.assertNotIn("ollama pull", "\n".join(calls))
        for model in MODELS:
            self.assertIn(f"ollama: {model} already present, skipping pull.", result.stdout)

    def test_only_the_missing_model_is_pulled(self):
        self.present("nomic-embed-text")
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {LLM}"])
        self.assertIn("ollama: nomic-embed-text already present, skipping pull.", result.stdout)

    def test_an_upgraded_volume_pulls_only_the_new_answering_model(self):
        # An install from before #647 holds phi3.5 and nomic-embed-text.
        self.present("phi3.5", "nomic-embed-text")
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {LLM}"])
        self.assertIn(f"ollama: {LLM} ready.", result.stdout)
        self.assertIn("ollama: nomic-embed-text already present, skipping pull.", result.stdout)
        self.assertNotIn("ollama pull phi3.5", "\n".join(calls))

    def test_another_variant_of_the_model_does_not_count_as_present(self):
        # A prefix match would take llama3.1:8b-instruct-q8_0 for llama3.1:8b (#647).
        self.present("llama3.1:8b-instruct-q8_0", "llama3.1:latest", "nomic-embed-text")
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {LLM}"])
        self.assertNotIn(f"ollama: {LLM} already present", result.stdout)

    def test_missing_models_are_pulled_under_a_two_hour_timeout(self):
        # The server isn't ready for the first two `ollama list` calls.
        self.tokens("list-fail", 2)
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {m}" for m in MODELS])
        self.assertEqual([c for c in calls if c.startswith("ollama pull")],
                         [f"ollama pull {m}" for m in MODELS])
        for model in MODELS:
            self.assertIn(f"ollama: {model} ready.", result.stdout)
        # Nothing is pulled before the server answers: two failed `ollama list` calls,
        # 2 s apart, come first.
        first_pull = calls.index(f"timeout 7200 ollama pull {LLM}")
        self.assertEqual(calls[:first_pull].count("sleep 2"), 2)

    def test_a_failed_or_timed_out_pull_is_retried_after_15_seconds(self):
        self.present("nomic-embed-text")
        self.tokens(f"hang-{LLM}", 1)   # the first attempt times out
        self.tokens(f"fail-{LLM}", 1)   # the second fails
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {LLM}"] * 3)
        self.assertEqual([c for c in calls if c == "sleep 15"], ["sleep 15"] * 2)
        for attempt in (1, 2):
            self.assertIn(f"ollama: pull of {LLM} failed or timed out (attempt {attempt}/5), "
                          "retrying in 15s...", result.stdout)
        self.assertIn(f"ollama: {LLM} ready.", result.stdout)

    def test_five_failed_attempts_stop_the_container(self):
        self.tokens(f"fail-{LLM}", 99)
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {LLM}"] * 5)
        self.assertEqual(calls.count("sleep 15"), 4)
        self.assertIn(f"ERROR: failed to pull {LLM} after 5 attempts.", result.stderr)
        # Nothing more is pulled.
        self.assertNotIn("nomic-embed-text", "\n".join(calls))


class ModelSettingsTests(EntrypointTestCase):
    """The models come from LLM_MODEL, BUILD_LLM_MODEL and ANSWER_LLM_MODEL (#671)."""

    def names(self, **settings):
        result, _calls = self.run_script(MODELS_SH, **settings)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_no_settings_resolve_to_the_defaults(self):
        self.assertEqual(self.names(), list(MODELS))
        self.assertEqual(self.names(LLM_MODEL="", BUILD_LLM_MODEL="", ANSWER_LLM_MODEL=""),
                         list(MODELS))

    def test_llm_model_is_the_default_for_both(self):
        self.assertEqual(self.names(LLM_MODEL="llama3.2"), ["llama3.2", "nomic-embed-text"])

    def test_each_setting_replaces_llm_model_for_its_task(self):
        self.assertEqual(self.names(LLM_MODEL="llama3.2", BUILD_LLM_MODEL=SMALL),
                         ["llama3.2", SMALL, "nomic-embed-text"])
        self.assertEqual(self.names(LLM_MODEL="llama3.2", ANSWER_LLM_MODEL=SMALL),
                         [SMALL, "llama3.2", "nomic-embed-text"])
        self.assertEqual(self.names(LLM_MODEL="x", BUILD_LLM_MODEL=SMALL, ANSWER_LLM_MODEL=LLM),
                         [LLM, SMALL, "nomic-embed-text"])

    def test_one_model_named_with_and_without_latest_is_pulled_once(self):
        self.assertEqual(self.names(BUILD_LLM_MODEL="llama3.2", ANSWER_LLM_MODEL="llama3.2:latest"),
                         ["llama3.2:latest", "nomic-embed-text"])
        # Another tag is another model.
        self.assertEqual(self.names(BUILD_LLM_MODEL="llama3.1"),
                         [LLM, "llama3.1", "nomic-embed-text"])

    def test_a_different_build_model_is_pulled_after_the_answer_model(self):
        self.present("nomic-embed-text")
        result, calls = self.run_entrypoint(BUILD_LLM_MODEL=SMALL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {LLM}",
                                             f"timeout 7200 ollama pull {SMALL}"])
        self.assertIn(f"ollama: {SMALL} ready.", result.stdout)

    def test_the_answer_model_setting_is_pulled_instead_of_llm_model(self):
        self.present("nomic-embed-text")
        result, calls = self.run_entrypoint(LLM_MODEL=LLM, ANSWER_LLM_MODEL=SMALL)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The build model is still LLM_MODEL, so both are pulled, the answer model first.
        self.assertEqual(self.pulls(calls), [f"timeout 7200 ollama pull {SMALL}",
                                             f"timeout 7200 ollama pull {LLM}"])

    def test_present_passes_only_when_every_resolved_model_is_listed(self):
        self.present(*MODELS)
        result, _ = self.run_script(MODELS_SH, "present")
        self.assertEqual(result.returncode, 0, result.stderr)
        result, _ = self.run_script(MODELS_SH, "present", BUILD_LLM_MODEL=SMALL)
        self.assertEqual(result.returncode, 1)
        self.present(SMALL)
        result, _ = self.run_script(MODELS_SH, "present", BUILD_LLM_MODEL=SMALL)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_present_uses_the_exact_name_rule(self):
        # Another variant of the name doesn't count (#647), as for the pulls.
        self.present("llama3.1:8b-instruct-q8_0", "nomic-embed-text")
        result, _ = self.run_script(MODELS_SH, "present")
        self.assertEqual(result.returncode, 1)

    def test_a_name_with_a_space_or_glob_character_is_refused(self):
        # The names are word-split, so they are checked as compose-models.sh checks them.
        # And the loose names (#772): "..", a leading "/" and a leading "-".
        for bad in ("two words", "llama*", "x?y", "a;b", "a..b", "../x", "x/../y", "/x", "-x"):
            with self.subTest(name=bad):
                result, _ = self.run_script(MODELS_SH, BUILD_LLM_MODEL=bad)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"models.sh: not a model name: '{bad}'", result.stderr)
                self.assertEqual(result.stdout, "")
        for good in ("someone/model:v1.2_x-y", "hf.co/user/model:Q4_K_M"):
            with self.subTest(name=good):
                result, _ = self.run_script(MODELS_SH, ANSWER_LLM_MODEL=good)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_entrypoint_stops_on_a_bad_name_before_any_pull(self):
        for bad in ("two words", "a..b", "-x"):
            with self.subTest(name=bad):
                result, calls = self.run_entrypoint(ANSWER_LLM_MODEL=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"models.sh: not a model name: '{bad}'", result.stderr)
                self.assertEqual(self.pulls(calls), [])

    def test_an_unknown_command_is_refused(self):
        result, _ = self.run_script(MODELS_SH, "pull")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: bash models.sh [list|present]", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
