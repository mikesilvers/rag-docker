```bash
python3 retrieve.py --username alice "your question here"
python3 retrieve.py --username alice --top-k 10 --mode hybrid "your question here"
python3 retrieve.py --username alice --api-url http://localhost:9090/api "your question here"
python3 retrieve.py --username alice --timeout 900 "a question that needs a long answer"
```

The API needs an account; any role may ask. The script asks for the password
(or reads it from standard input with `--password-stdin`), logs in, asks and
logs out. The password is never a flag or an environment variable;
`RAG_USERNAME` may stand in for `--username`.

It needs Python 3 and nothing else — no `pip install` — because the air-gapped
case is the one this project targets. It exits 0 on success, 2 on bad usage,
3 if the API is unreachable or times out, 4 if the collection is absent and 5 if
the API returns an error.

A package carries `retrieve.py` only when the collection had retrieval settings
saved. A script claiming tuned parameters while carrying stock defaults would be
worse than no script.
