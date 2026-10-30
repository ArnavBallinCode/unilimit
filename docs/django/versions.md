# Supported versions

| Component | Supported | Tested in CI |
|---|---|---|
| Python | 3.11 – 3.14 | 3.11, 3.12, 3.13, 3.14 |
| Django | 5.2 LTS, 6.0, 6.1 | see below |
| Django REST Framework | 3.16 and later | see below |
| Redis / Valkey | as for the core: Redis 6.2+, Valkey 8 | standalone and Cluster |

The exact Django and DRF versions the test suite last ran against are recorded in
`context/status.md`. Django versions older than 5.2 are not supported: the integration
relies on `request.auser()` and on async middleware semantics of current releases.

Install with the extras for the pieces you use:

```bash
pip install "unilimit[django]"       # Django only
pip install "unilimit[drf]"          # Django and Django REST Framework
pip install "unilimit[all]"          # everything, including Redis and FastAPI
```

The Django integration never imports DRF, and the core package never imports Django:
installing `unilimit[django]` does not pull in DRF.
