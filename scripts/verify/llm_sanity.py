"""Does the LLM still give sensible answers? Run inside the api container.

A degraded Ollama runner can keep listing its models (so /health stays ok)
while every chat reply is garbage: mixed scripts, fragments of unrelated
instructions, thousands of characters, or a timeout (#119). Every LLM check
then fails for reasons that have nothing to do with the code under test.

This asks one question with a known answer and judges only the shape of the
reply. Exit 0: sensible. Exit 1: degraded. Exit 2: Ollama didn't answer.
"""
import sys

import httpx

from config import settings

QUESTION = ("Policy: department managers approve overtime. "
            "Question: who approves overtime? Answer in one sentence.")
TIMEOUT_S = 120
MAX_CHARS = 600

try:
    resp = httpx.post(
        f"http://{settings.ollama_host}:{settings.ollama_port}/api/chat",
        json={"model": settings.llm_model, "stream": False,
              "options": {"temperature": 0, "num_predict": 80},
              "messages": [{"role": "user", "content": QUESTION}]},
        timeout=TIMEOUT_S)
    resp.raise_for_status()
    answer = resp.json()["message"]["content"].strip()
except Exception as exc:  # unreachable, timed out, or not JSON
    print(f"no answer from {settings.llm_model}: {type(exc).__name__}: {exc}")
    sys.exit(2)

printable = sum(1 for ch in answer if ch.isascii() and (ch.isprintable() or ch in "\n\r\t"))
problems = []
if not answer:
    problems.append("empty reply")
if len(answer) > MAX_CHARS:
    problems.append(f"{len(answer)} characters for a one-sentence question")
if answer and printable / len(answer) < 0.9:
    problems.append("mostly non-ASCII text")
if "manager" not in answer.lower():
    problems.append("doesn't mention managers")

if problems:
    print(f"{settings.llm_model} looks degraded ({'; '.join(problems)}): {answer[:200]!r}")
    sys.exit(1)
print(f"{settings.llm_model} answers sensibly: {answer[:120]!r}")
