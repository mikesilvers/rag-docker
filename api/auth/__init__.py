"""Accounts, sessions, service API keys and the API's endpoint/role matrix (#25).

- `passwords`: the username and password rules, scrypt hashing, generated passwords.
- `store`: the SQLite file in AUTH_DIR holding accounts, service principals and
  their API keys, sessions and the login throttle.
- `keys`: the service API key format (`Authorization: Bearer rag_<id>_<secret>`).
- `policy`: which role each route needs.
- `middleware`: enforces the policy on every request, before the route sees its body.
- `__main__`: the operator command, `python -m auth`, which creates the first
  Engineer and recovers lockouts; Engineers manage accounts in the app too
  (routers/auth.py, the Users page) and service principals and their keys
  (the Service keys page).
- `testing`: a real account and session in a temporary store, for in-process tests.
"""
