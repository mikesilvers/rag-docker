"""Accounts, sessions and the API's endpoint/role matrix (#25).

- `passwords`: the username and password rules, scrypt hashing, generated passwords.
- `store`: the SQLite file in AUTH_DIR holding accounts, sessions and the login throttle.
- `policy`: which role each route needs.
- `middleware`: enforces the policy on every request, before the route sees its body.
- `__main__`: the operator command, `python -m auth`, which creates the first
  Engineer and recovers lockouts; Engineers manage accounts in the app too
  (routers/auth.py, the Users page).
- `testing`: a real account and session in a temporary store, for in-process tests.
"""
