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

## Steps

1. **List the testable considerations.** Start from the requirements ledger. Add every behaviour change, error path, boundary value, status transition and UI state in the diff. Number them T1, T2, …
2. **Map them to tests.** For each T, find the test in the PR or in the existing suites that exercises it, and cite `file:line`. A T with no test is a gap.
3. **Write the missing tests** in the merged worktree (`merged/`), in the project's style. Tests for a bug fix must fail on the base and pass on the PR head.
4. **Run.** Only when the PR is same-repository, or the coordinator has confirmed the user's go-ahead. Run everything on the **evaluated commit**, in the merged worktree (`merged/`, see `reference.md`): that's the PR as it would merge into the current `develop`. Never build or run the PR's head as it stands.
   - Run on the **verify project**, never on the live `rag-docker` stack, with the main checkout's `stack.sh` after checking it against `origin/develop` ("The verify project" in `reference.md`): `bash <main>/scripts/verify/stack.sh run --checkout <bundle>/merged [suites]`. Each run builds every image from that checkout (unchanged images come from the cache), starts it on fresh, empty volumes, runs that checkout's `all.sh` (only the named suites, if you name any) and tears the project down. Never run `docker compose` without `-p rag-verify`, and never with `-p rag-docker`.
   - Run the suites for the changed areas (for example `stack.sh run --checkout <bundle>/merged 02 04`), then the whole of `all.sh`. Use the full run when the PR touches ingest, query, gold standard or Ollama; `RAG_SKIP_SLOW=1` is enough otherwise. Add `RAG_ALLOW_RESTART=1` when the issue concerns restart behaviour; on the verify project, restarts are harmless.
   - For a bug fix, run the new tests on the base as well, to show they fail there. The base is `develop` at the develop SHA the coordinator gave you: `git worktree add --detach <bundle>/base <develop-sha>`, then `stack.sh run --checkout <bundle>/base [suites]`. Every run starts on fresh volumes, so the base never runs on state the PR's code wrote (#78). **Cost per evaluation:** each base run adds one build and start of the verify project and one teardown to the suite's own time (about 19 minutes full, 8 minutes with `RAG_SKIP_SLOW=1`). Measured for #152 with cached images: about 40 seconds to start, including the sha256 check of the model copy (the first `up` on a machine also copies the models, about 20 seconds more), and about 5 seconds to tear down.
   - A check that fails may be re-run once; see "Flaky failures" below.
   - Wait for long runs as "Waiting for long runs" below says.
   - The PR's own changes to the harness (`scripts/verify/stack.sh`, `scripts/verify/lock.sh`, `docker-compose.verify.yml`) don't run here: list each as a consideration with the result `➖ not testable in the evaluation (trusted harness)`. The maintainer may run the PR's copy after review.
   - `stack.sh run` always tears the verify project down. Confirm it as "The verify project" in `reference.md` says. Only then remove a `base/` worktree you created. Never remove `worktree/` or `merged/`: the coordinator's build check runs from `merged/` after you, and the coordinator removes both.
5. **Loop.** Follow the review loop in `reference.md` until every T is covered and has been run.

## Waiting for long runs

A full `all.sh` takes 10–25 minutes, longer than one tool call may run. **Never end your turn while a run is in progress**, and never rely on a notification, monitor or watcher to resume you: when your turn ends, nothing is guaranteed to wake you, and the evaluation stalls.

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

**3. A check outside the PR gets one re-run, in this evaluation.** Re-run only that check, or the smallest suite that contains it; never the whole of `all.sh`. For the answer-length check in `03_query.sh`, re-run with `RAG_FORMAT_TRIALS=9` (documented in `scripts/verify/README.md`): more trials make the mean steadier.
- **Passes on the re-run:** Medium (flaky). Name both runs' results, and put `(flaky)` in the finding's first line, so the coordinator shows it next to the verdict.
- **Fails again:** run `develop`'s own copy of that suite on the base (the develop SHA the coordinator gave you, from a `base/` worktree as in step 4).
  - **Fails on the base too:** it isn't the PR's. Report it as a Medium on `develop`.
  - **Passes on the base:** Medium (environmental), per the maintainer's rulings on #65 and #67 (#94's Decision): the check is outside the PR, so its failing twice here says more about the machine than the PR. Record all three results, say plainly that it failed twice on the evaluated commit, and put `(environmental)` in the finding's first line, so the coordinator shows it next to the verdict.

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

```markdown
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

Write `findings-tests.json` per `reference.md`, with the sections above appended to your recap section, then return the result block. If you weren't allowed to run code, return `verdict: NOT RUN` and still write the coverage mapping. Post nothing on GitHub and apply no labels: the coordinator posts one recap at the end.
