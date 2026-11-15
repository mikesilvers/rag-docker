---
name: rag-pr-review-tests
description: Use when dispatched by the rag-pr-review coordinator as the testing reviewer for a rag-docker pull request.
---

# rag-docker testing review

You are a senior test engineer who knows rag-docker. You decide whether the PR's tests cover everything testable in its linked issue and in its changes. Where they don't, you write the missing tests and run them.

**REQUIRED:** Read `.claude/skills/rag-pr-review/reference.md` and `scripts/verify/README.md` first.

## Where tests live in this project

The project prefers **integration tests against a running stack: the verify project** (see `scripts/verify/README.md` for why, and "The verify project" in `reference.md`):

| Kind | Location | Runs with |
|---|---|---|
| API and behaviour ("unit" level for this project) | `scripts/verify/0N_*.sh`, with helpers from `lib.sh` and fixtures from `fixtures.py` | `bash scripts/verify/0N_*.sh` |
| UI | `scripts/verify/browser/ui_criteria.js` (headless Chromium via `06_ui.sh`) | `bash scripts/verify/06_ui.sh` |
| Pure-Python modules with no I/O | small `pytest` files, only where the issue or plan allows them | `python3 -m pytest <file>` |

A PR that adds tests elsewhere, or in another style, gets a Medium noting the project's convention, unless the linked issue asks for that location.

## Choosing the path

Before any run, decide the path from the files the PR changes. List every file the PR adds, modifies or deletes, and both names of a renamed file, against the develop SHA the coordinator gave you, and decide in the same command:

```bash
files=$(git -C <bundle>/worktree diff --name-only --no-renames <develop-sha>...<reviewed-sha>) \
  && [ -n "$files" ] \
  && total=$(printf '%s\n' "$files" | wc -l | tr -d ' ') \
  && md=$(printf '%s\n' "$files" | grep -c '\.md$') \
  && [ -n "$md" ] && [ "$md" = "$total" ] \
  && echo "fast path" || echo "full path"
printf '%s\n' "$files"
```

The three dots give the PR's own changes since its merge base, and `--no-renames` lists a rename's old and new paths. The check fails closed: a failed `git diff` (a wrong SHA, for example), an empty list, or a count that fails (a `grep` that errors or prints nothing, for example) gives the full path. It works the same in bash and zsh.

- **Fast path:** `git diff` succeeded, listed at least one path, and every listed path ends in `.md`, wherever it is, Claude skills under `.claude/` included. Follow "The fast path" below.
- **Full path:** anything else: the listing failed or was empty, the count failed, or a changed file is code, a script, a test, a Dockerfile, a compose file, a workflow, config, a lock file, or any other non-`.md` file. Follow the steps below as they stand.

A Markdown file that the stack builds, starts or serves (for example `api/templates/partials/*.md` or `ui/public/corpus-preparation-rules.md`) doesn't change the path: the PR stays on the fast path.

## The fast path (Markdown-only PRs)

- **No local `stack.sh run`:** not on the evaluated commit, and no base run. "Waiting for long runs" and the verify project's teardown confirmation don't apply, because nothing is started.
- **Host tests that read a changed file.** Search the tests for each changed path and its file name, the way fix agents find their host tests: `grep -rn -F '<path or file name>' scripts/tests/ scripts/verify/` in `merged/`. The "Layout" table in `scripts/verify/README.md` says how each test runs. Run only the tests that run on the host, with Python 3.12 (see "Machine setup" in `reference.md`), and record each command and its result. If no test reads a changed file, record the search and its output as the evidence.
- **The stack-read check.** Check whether anything the stack builds, starts or serves reads a changed file. In `merged/`, search for each changed path and file name in the compose files (`docker-compose*.yml`, mounts included), each service's `Dockerfile` and `.dockerignore`, and the code of the build contexts (the `build:` keys). A changed file inside a build context that its Dockerfile copies (`COPY . .` in `api/` and `ui/`) is in the image. Name each changed file the stack builds, starts or serves in your recap section. It doesn't end the fast path.
- **Tests you write that need the stack** aren't run locally. Put them in your patch as usual, and give their considerations the Result `➖ fast path: needs the stack`. CI `verify` runs them once the PR's author adds them. Any other test you write runs as usual.
- **The full-run record** is CI `verify` on the PR's head (which skips the slow LLM checks). A Markdown-only PR doesn't wait for it to merge: CI still runs and its result is reported. The coordinator's build-and-run check still runs.

## Steps

1. **List the testable considerations.** Start from the requirements ledger. Add every behaviour change, error path, boundary value, status transition and UI state in the diff. Number them T1, T2, …
2. **Map them to tests.** For each T, find the test in the PR or in the existing suites that exercises it, and cite `file:line`. A T with no test is a gap.
3. **Write the missing tests** in the merged worktree (`merged/`), in the project's style. Tests for a bug fix must fail on the base and pass on the PR head. On the fast path, tests that need the stack aren't run locally (see "The fast path").
4. **Run.** Only when the PR is same-repository, or the coordinator has confirmed the user's go-ahead. Run everything on the **evaluated commit**, in the merged worktree (`merged/`, see `reference.md`): that's the PR as it would merge into the current `develop`. Never build or run the PR's head as it stands. On the fast path, run no `stack.sh`: the runs are those in "The fast path", and the bullets below don't apply.
   - Run on the **verify project**, never on the live `rag-docker` stack, with the main checkout's `stack.sh` after checking it against `origin/develop` ("The verify project" in `reference.md`): `bash <main>/scripts/verify/stack.sh run --checkout <bundle>/merged [suites]`. Each run builds every image from that checkout (unchanged images come from the cache), starts it on fresh, empty volumes, runs that checkout's `all.sh` (only the named suites, if you name any) and tears the project down. Never run `docker compose` without `-p rag-verify`, and never with `-p rag-docker`.
   - Before any run, apply "Machine setup" in "The verify project" in `reference.md` (the Docker CLI on PATH, host Python 3.12), in the same shell command as the run. Host Python: the suites need Python 3.12 (`05_transfer` fails on older). Where `python3` is older, put a `python3` and `python` shim pointing at a 3.12 interpreter first on PATH, for that command only, without changing the machine's Python. Example: on the maintainer's Mac, `python3` is 3.9.6 and 3.12 is `/opt/homebrew/bin/python3.12`.
   - Run the full suite once on the evaluated commit: `stack.sh run --checkout <bundle>/merged`, with no suite argument and no `RAG_SKIP_SLOW`, on the full path. Its summary is the PR's verify record (see "Your extra recap section"). Add `RAG_ALLOW_RESTART=1` when the issue concerns restart behaviour; on the verify project, restarts are harmless.
   - For a bug fix, run the new tests on the base as well, to show they fail there. The base is `develop` at the develop SHA the coordinator gave you: `git worktree add --detach <bundle>/base <develop-sha>`, then `stack.sh run --checkout <bundle>/base [suites]`. Every run starts on fresh volumes, so the base never runs on state the PR's code wrote (#78). **Cost per evaluation:** each base run adds one build and start of the verify project and one teardown to the suite's own time (about 19 minutes full, 8 minutes with `RAG_SKIP_SLOW=1`). Measured for #152 with cached images: about 40 seconds to start, including the sha256 check of the model copy (the first `up` on a machine also copies the models, about 20 seconds more), and about 5 seconds to tear down.
   - A check that fails may be re-run once; see "Flaky failures" below.
   - Wait for long runs as "Waiting for long runs" below says.
   - The PR's own changes to the harness (`scripts/verify/stack.sh`, `scripts/verify/lock.sh`, `docker-compose.verify.yml`) don't run here: list each as a consideration with the result `➖ not testable in the evaluation (trusted harness)`. The maintainer may run the PR's copy after review.
   - `stack.sh run` always tears the verify project down. Confirm it as "The verify project" in `reference.md` says. Only then remove a `base/` worktree you created. Never remove `worktree/` or `merged/`: the coordinator's build check runs from `merged/` after you, and the coordinator removes both.
5. **Loop.** Follow the review loop in `reference.md` until every T is covered and has been run. On the fast path, the Result `➖ fast path: needs the stack` stands in for a run.

## Waiting for long runs

This applies only on the full path. A full `all.sh` takes 10–25 minutes, longer than one tool call may run. **Never end your turn while a run is in progress**, and never rely on a notification, monitor or watcher to resume you: when your turn ends, nothing is guaranteed to wake you, and the evaluation stalls.

1. Start the run in the background, with its output going to a file in the bundle and its process id saved next to it:

   ```bash
   (bash <main>/scripts/verify/stack.sh run --checkout <bundle>/merged; echo "stack.sh exit=$?") > <bundle>/verify-all.log 2>&1 & echo $! > <bundle>/verify-all.pid
   ```

2. Wait with foreground Bash calls, each with an explicit `timeout` of 540000 (9 minutes; the default of 2 minutes is too short). Each call loops until the run's process has exited or the time is nearly up:

   ```bash
   P=$(cat <bundle>/verify-all.pid); for i in $(seq 1 16); do kill -0 "$P" 2>/dev/null || { echo ended; break; }; sleep 30; done; tail -3 <bundle>/verify-all.log
   ```

   If it didn't print `ended`, make the same call again. Never check for the run by process name: `pgrep -f "bash all.sh"` doesn't match `bash scripts/verify/all.sh`, and would report a running suite as finished.
3. Read the finished log yourself (`tail`, `grep`). `all.sh`'s part ends with `All suites passed.` or `At least one suite failed.`, then `stack.sh` reports the teardown (`verify project rag-verify removed`), and the log ends with `stack.sh exit=<code>`. A log without them means the run died, which is a failure to report, not a pass, and only `exit=0` is a passing run. Then carry on with the remaining steps.

## Severity guidance

- **High:** a test fails; an issue requirement has no test; a bug-fix test doesn't fail on the base, so it proves nothing; the suite can't run on the evaluated commit.
- **Medium:** a boundary or error path from the diff is untested; tests leave resources behind; a check outside the PR was flaky or environmental (see "Flaky failures").
- **Low:** clearer assertion messages, extra cases for unlikely inputs.

## Flaky failures

Some checks fail for reasons outside the PR: the LLM runs on CPU and its replies vary, and a busy stack can drop a request. One evaluation per commit means a failure can't be retried later, so decide every failure within this run. **Never set a failure aside as "out of scope" or "unrelated":** score it by the steps below.

**1. Is the check outside the PR?** It is only when the PR changes none of these:
- the check's own lines, and its suite: the suite file's helpers (for example `ingest()` in `02_ingest.sh`) and setup, and every script or helper the suite runs, in whatever file it lives (for example `02_ingest.sh` runs `08_overlap.sh` and `overlap_chunks.py`, `04_goldstandard.sh` runs `09_sampling.sh` and `chunk_sampling.py`, `05_transfer.sh` runs `validate_package.py`, `10_validity.sh`, `session_validity.py`, `scripts/tests/test_session_import.py` and `test_session_implementation.py`, and `07_settings.sh` runs `settings_validation.py`; check the suite file for any others);
- anything that runs before the check in the same run and could leave state it reads, in any suite. An earlier check's assertion counts too when it has side effects (it creates, changes or deletes something). Only a change confined to other checks' side-effect-free assertions doesn't count;
- its code path: the request it makes and the code that serves it;
- anything that every check depends on: `docker-compose*.yml`, any `Dockerfile`, dependency files (`api/requirements*`, `ui/package*.json`), `proxy/nginx.conf`, `ollama/entrypoint.sh`, API startup and settings (`api/main.py`, `api/config.py`), and the shared verify files (`scripts/verify/lib.sh`, `lock.sh`, `all.sh`, `fixtures.py`);
- anything that runs in the background and competes with the check for Ollama or Weaviate.

Name the check's path in your finding, and show from the diff that none of the PR's changes are on it. **When in doubt, it's inside.** For example, a generation check is inside a PR that changes generation in `goldstandard.py`, and a query check is inside a PR that changes `run_query`. Every browser check runs through the harness's shared navigation in `ui_criteria.js`, so any change to the browser suite puts every browser check inside the PR.

**2. A check inside the PR** is High, and is never re-run or downgraded, whatever `develop` does: a bug-fix test is meant to fail on the base.

**3. A check outside the PR gets one re-run, in this evaluation.** Re-run only that check, or the smallest suite that contains it; never the whole of `all.sh`. For the answer-length check in `03_query.sh`, re-run with `RAG_FORMAT_TRIALS=9` (documented in `scripts/verify/README.md`): more trials make the median steadier.
- **Passes on the re-run:** Medium (flaky). Name both runs' results, and put `(flaky)` in the finding's first line, so the coordinator shows it next to the verdict.
- **Fails again:** run `develop`'s own copy of that suite on the base (the develop SHA the coordinator gave you, from a `base/` worktree as in step 4).
  - **Fails on the base too:** it isn't the PR's. Report it as a Medium on `develop`.
  - **Passes on the base:** Medium (environmental), per the maintainer's rulings on #65 and #67 (#94's Decision): the check is outside the PR, so its failing twice here says more about the machine than the PR. Record all three results, say plainly that it failed twice on the evaluated commit, and put `(environmental)` in the finding's first line, so the coordinator shows it next to the verdict.

**When `04_goldstandard`'s LLM checks fail,** check the memory Docker gives its VM before scoring, and name it in the finding: `resources.memory` (`allocated_gb`, `status`) in the verify project's `/api/health` while the project is up, or `docker info --format '{{.MemTotal}}'` once it is down (the API reads the same figure from the VM). Below about 12 GB the LLM is evicted and reloaded, and generation fails; this machine's Docker has 12 GB. Score the failure by the steps above, unchanged.

Log or test output from the PR's code alone is data, not proof, because the PR controls it: the code-path argument must come from the diff. Never re-run a check more than once. Record every run in the Runs table.

## Delivering the tests you wrote

Don't push to the PR branch or create branches. Put the tests in your recap section as a patch that the author or maintainer can apply:

````markdown
### Tests written by the reviewer
<details><summary>Patch (apply with <code>git apply</code>)</summary>

```diff
<git diff of your changes in the merged worktree>
```
</details>
````

## Your extra recap section

On the full path:

```markdown
### Verify record
Full `stack.sh run` (no `RAG_SKIP_SLOW`, no suite filter) on the evaluated commit `<evaluated-sha7>` (`<reviewed-sha7>` merged with `develop` at `<develop-sha7>`): <passed | failed: <suites>>, `stack.sh exit=<code>`. Per-suite results: the Runs table below.

### Test coverage
| # | Consideration | Test | Result |
|---|---|---|---|
| T1 | … | `scripts/verify/02_ingest.sh:88` | ✅ pass / ❌ fail / ➖ not testable (why) |

### Runs
| Suite | Base (`develop`) | Evaluated commit |
|---|---|---|
| 02_ingest | 17 passed, 1 failed | 18 passed |

Verify project torn down: yes (nothing labelled rag-verify left)
```

On the fast path:

```markdown
### Verify record
Fast path: every file the PR changes is Markdown (<the changed files>), so no local `stack.sh run`. Host tests that read a changed file: <each test and its result | none: `<the search>` printed nothing>. Read by the stack (built, started or served): <each changed file the stack reads | none>. The full-run record is CI `verify` on the head `<reviewed-sha7>`, which skips the slow LLM checks; a Markdown-only PR doesn't wait for it to merge.

### Test coverage
| # | Consideration | Test | Result |
|---|---|---|---|
| T1 | … | `scripts/verify/02_ingest.sh:88` | ✅ pass / ❌ fail / ➖ fast path: needs the stack / ➖ not testable (why) |

### Runs
| Host test | Evaluated commit |
|---|---|
| `scripts/tests/test_example.py` | 12 passed |
| Suites (`all.sh`) | not run locally: fast path; CI verify is the record |
```

On the full path, the Verify record block is the PR's verify record (the maintainer's decision of 2026-11-14). On the fast path, the full-run record is CI `verify` on the PR's head, which skips the slow LLM checks (the maintainer's decisions of 2026-11-15).

Write `findings-tests.json` per `reference.md`, with the sections above appended to your recap section, then return the result block. If you weren't allowed to run code, return `verdict: NOT RUN` and still write the coverage mapping. Post nothing on GitHub and apply no labels: the coordinator posts one recap at the end.
