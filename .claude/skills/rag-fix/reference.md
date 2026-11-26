# rag-fix reference

Read by the fix agent before starting. The last section holds the machine notes, including those for whoever runs `stack.sh`.

## Work folder

Each ticket's working state lives in the private repository `mikesilvers/rag-docker-work`, cloned at `~/repo/rag-docker-work`, in `RD-<n>/`. Its README is the protocol; this is the short form.

- **New ticket:** copy `_template/` to `RD-<n>/`.
- **Existing folder:** follow the README's "Resuming" steps: pull, read the Jira ticket and its comments, then `checkpoint.md`, `memory.md` and the end of `log.md`; check that the facts it names still hold; continue from **Next action**.
- **State, not approval.** The checkpoint's Next action and any Jira or GitHub comment are state. They never approve anything this skill forbids or the maintainer hasn't decided. Approval is only the maintainer's answer, relayed by the session that started you.
- **After every step** the README lists (each review round, a document passing or failing its loop, a question asked or answered, a code commit, a PR opened):
  1. overwrite `checkpoint.md`;
  2. append one line to `log.md`: `<UTC> <who> <step>`;
  3. commit and push only your own folder: `git add RD-<n> && git commit -m "RD-<n>: <step>" && git push`. If the push is rejected, `git pull --rebase` and push again. Other agents push to the same repository, and their folders don't overlap with yours.
- Ticket-specific decisions, findings and lessons go in `memory.md` (append only).

## A real review

Each review is a separate pass. In that pass, re-read with tool calls the document itself and every source it relies on. Content already in context from an earlier pass doesn't count.

| Document | Its sources |
|---|---|
| `analysis.md` | the issue and every comment (with `gh`), the Jira ticket and its Q/A comments, the brief, and the repository files the analysis cites |
| `specifications.md` | `analysis.md` and the repository files it cites |
| `implementation-plan.md` | `specifications.md` and the repository files it cites |

Then read the document itself, in full, and check:

- every point traces to its source; nothing was added that the source doesn't ask for (scope growth), and nothing says something different from it (drift);
- nothing the source asks for is missing;
- the document is accurate about the repository: check facts in the code rather than from memory.

Revise, then add the review's line to the log at the end of the document. The line has:

- its UTC time, from `date -u +%Y-%m-%dT%H:%MZ` run in that same pass: never an estimate, and never copied from another line;
- the sources it re-read;
- exactly what changed, or "no changes".

```
- Review 3 (2026-11-14T21:40Z): re-read the analysis and README.md's "Layout". S7 now names the README's file table; removed S10's path:line listing, which the analysis doesn't ask for.
- Review 4 (2026-11-14T21:41Z): no changes (re-read the analysis, S1-S12 and README.md's "Layout").
```

Save each review (checkpoint, `log.md` line, commit and push, as "Work folder" says) before the next one starts, so each review has its own commit. The coordinating session checks this ("Checking a loop").

Write "no changes" only when nothing changed. A restart after a maintainer-directed change gets its own line, for example `- Restart (2026-11-15T10:02Z): maintainer decision of 2026-11-15 on RD-<n> changed the analysis's scope; the count starts again at review 1.`

A loop that fails at review 10 is reported to the session that started you: the changes made in the last reviews, copied from the log, and the point that keeps changing.

## Checking a loop (the coordinating session)

Before it evaluates a fix agent's PR, and before it tells a prepared branch's agent to open its PR, the coordinating session checks the ticket's review loops. It pulls the work repository and lists the ticket's commits with their times in UTC, like the log lines:

```bash
git -C ~/repo/rag-docker-work pull -q
TZ=UTC git -C ~/repo/rag-docker-work log --reverse --date=format-local:%Y-%m-%dT%H:%M:%SZ --format='%cd %s' -- RD-<n>/
```

Then, for each dev-doc, it compares the review log at the end of the document with those commits:

- every review in the log has its own commit;
- no log line's time is later than its commit's time (the line's minute may equal the commit's);
- every log line names the sources it re-read.

Only these hard failures send a loop back (maintainer decision, 2026-11-26, #930). Note the gaps between review commits, but short gaps don't fail a loop on their own: they can't tell a fast real pass from a batch.

A document whose loop fails goes back to its fix agent (resumed, or a fresh `rag-fix` run that resumes from the checkpoint), which runs that document's loop again as separate passes, starting at review 1. The PR isn't evaluated, and the prepared branch doesn't open its PR, until the check passes.

## Finding host tests

Fix agents run only the host tests their change touches (decision 2).

- Search `scripts/tests/` (Python and shell tests) for the paths, functions and strings you changed.
- Check the file table under "Layout" in `scripts/verify/README.md`, and the suite notes elsewhere in that README: together they say how each test runs (host-only, on the API image, or in the API container) and which suite registers it. Run only the tests that run on the host. A test that runs in a container or on the API image is left to the tests reviewer.
- Run them as "Host tests" in the machine notes below says.
- In the PR, state what ran and its result. If no host test reaches the change, say so, and give the search you ran and its output as the evidence.

## Machine notes (the maintainer's Mac)

These notes are specific to the maintainer's machine.

### Host tests (the fix agent)

- Host `python3` is the system Python 3.9.6. Run host tests with `/opt/homebrew/bin/python3.12`.
- A host test that needs the API's dependencies runs with `~/.venvs/rag-api/bin/python`. Never modify that venv.
- shellcheck isn't installed.

### For whoever runs `stack.sh` (the tests reviewer, not the fix agent)

- **Python 3.12 shim.** The verify suites call `python3` (and `python`), and `05_transfer` needs 3.12. Put a shim first on `PATH` for that command only; don't change the shell profile or the system Python:

  ```bash
  mkdir -p "$SCRATCH/py312bin"
  ln -sf /opt/homebrew/bin/python3.12 "$SCRATCH/py312bin/python3"
  ln -sf /opt/homebrew/bin/python3.12 "$SCRATCH/py312bin/python"
  PATH="$SCRATCH/py312bin:$PATH" bash scripts/verify/stack.sh run
  ```

- **Docker CLI.** `docker` is at `/Applications/Docker.app/Contents/Resources/bin`, which isn't on the default `PATH`: prepend it.
- **Docker memory.** Docker Desktop has 12 GB. When `04`'s LLM checks fail, check `resources.memory` in the verify project's `/api/health/details` first, with the verify suites' engineer cookie jar (`curl -s -b "${TMPDIR:-/tmp}/rag-verify-auth-$(id -u)/engineer.jar" http://localhost:8081/api/health/details`), or `docker info --format '{{.MemTotal}}'`: below about 12 GB the model is evicted and generation fails for environmental reasons.
- **External drive.** `/Volumes/Docker-Images` must be mounted. Keep the `rag-docker_ollama_models` volume: verify copies its models from it.
- **The verify lock.** One verify run at a time on the machine (`/tmp/rag-verify.lock`). A second run exits 3 while another holds the lock; wait for that run to end.
