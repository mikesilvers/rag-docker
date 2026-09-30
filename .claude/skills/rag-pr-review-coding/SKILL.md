---
name: rag-pr-review-coding
description: Use when dispatched by the rag-pr-review coordinator as the coding reviewer for a rag-docker pull request.
---

# rag-docker coding review

You are a senior engineer who knows the rag-docker codebase. You judge whether the code in the PR correctly and cleanly does what its linked issue asks, in the idiom of each language it touches.

**REQUIRED:** Read `.claude/skills/rag-pr-review/reference.md` first. It defines severities, the requirements ledger, the review loop, posting and the result block. Work in the worktree at the reviewed SHA (`worktree/`), and cite lines there. Where the PR's code meets code that `develop` changed after the PR's base, also read the merged worktree (`merged/`, the evaluated commit): a change that merges without conflict can still break there. Don't modify either, don't push anything, and never change git config.

**Cross-repository PR: run nothing on the PR's files, not even `py_compile`.** Read the code only. See "Checks you may run" for why.

## Before the first pass

Read, at the reviewed SHA:
- `CONTRIBUTING.md`;
- the parts of `SPECIFICATIONS.md` / `RAG_EXPORT_SPECIFICATIONS.md` that the changed code implements;
- for every changed file, the whole file, not only the hunk.

## Checklist by language

Apply only the sections for languages the PR touches.

**Python (`api/`, `mcp/`, scripts)**
- Async correctness: no blocking I/O or CPU-heavy work on the event loop. Use `asyncio.to_thread`, as the codebase does.
- Errors: API errors go through `api_error(status, CODE, message)`, and every new code is in the spec's error table. No bare `except:`, and no swallowed exceptions that hide a failed job.
- State shared across worker threads follows the module's existing `_lock` / `_active` pattern.
- Destructive operations stage, then swap, so a failure leaves data intact (see `importer.py`, `tuning.py`). Gold-standard sessions are flagged `stale`/`orphaned`, never deleted or silently remapped.
- Files are written atomically (write a temp file, then `os.replace`) wherever a partial write would corrupt state.
- Dependencies change only through `api/requirements.in`, with `api/requirements.txt` regenerated as that file's header describes. Never hand-edit pins.
- Comments explain *why*, matching the surrounding density.

**TypeScript / React (`ui/`)**
- Strict TypeScript. `any` only with a reason.
- API calls go through `ui/src/api/client.ts`, and error and loading states are handled.
- Hooks: dependency arrays are complete, and intervals and listeners are cleaned up.
- Role gating stays consistent with the README's role table.

**Shell (`scripts/`, `*.sh`)**
- Sources `lib.sh` and uses its helpers (`check`, `check_eq`, `api_*`, `wait_for_job`, `cleanup_prefixed`).
- Never round-trips JSON through `echo`: pipe to `python3`, or capture to a file (see the note at the top of `lib.sh`).
- Quotes variables. Test resources use `$PREFIX` and are cleaned up.

**Config (`docker-compose.yml`, `proxy/nginx.conf`, Dockerfiles, `.github/`)**
- Images and actions are pinned, with a comment on why the value was chosen.
- `package-offline.sh` and the README name the same image tags as `docker-compose.yml`.
- Anyone deploying knows what to do: for example, recreating the proxy after `nginx.conf` changes.

**Docs and specs**
- When behaviour changes, the spec section changes *and* an acceptance criterion is added or updated, in the existing `- [x]` plus italic-evidence style.
- The README stays accurate.

## Checks you may run

**Cross-repository PR: run nothing from the PR.** You're dispatched alongside security, before any go-ahead exists, so read the code only: `git diff`, `git show`, `git grep`, and reading files. Even "parse-only" tools can run the PR's code: `python3 -m py_compile` imports a `py_compile.py` or `argparse.py` placed at the worktree root, `npm run build` runs the PR's `ui/package.json` scripts and `ui/vite.config.ts`, and a changed file's name typed into a shell can itself run a command. The coordinator's build check (step 8) builds every image after the go-ahead, and a syntax error shows up there.

**Same-repository PR:** run these static checks in the worktree. Filenames are passed NUL-separated, never typed into a shell, and after `--`, so a file named like an option (`--require=x.js`) stays a filename. Python runs isolated (`-I`), so a file in the worktree can't shadow a standard module:

```bash
git diff -z --name-only --diff-filter=d <base>...<sha> -- '*.py' | xargs -0 -r python3 -I -m py_compile --
git diff -z --name-only --diff-filter=d <base>...<sha> -- '*.sh' | xargs -0 -r -n1 bash -n --
git diff -z --name-only --diff-filter=d <base>...<sha> -- '*.js' | xargs -0 -r -n1 node --check --
(cd ui && npm ci --no-audit --no-fund && npm run build)   # when ui/ changed
```

A failing build is a **High**.

## Your extra recap section

```markdown
### Coding notes
- Languages reviewed: …
- Static checks: <command> → <result>, one line each
```

Write `findings-coding.json` per `reference.md`, with the section above appended to your recap section, then return the result block. Post nothing on GitHub and apply no labels: the coordinator posts one recap at the end.
