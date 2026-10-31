# Rate limiting from scratch

A client (a browser, phone, or another program) sends an HTTP request to a server.
The server chooses a handler from the URL, performs work, and returns an HTTP response.
A rate limiter decides whether that work may begin: for example, no more than ten
searches per minute for one client. A rejected HTTP request usually gets status **429**;
`Retry-After` tells the client when to retry. Clients should back off rather than retry
in a tight loop.

## What does the limiter remember?

A **rule** describes the budget, such as `10/minute`. An **identity** describes who
shares it: an IP address, authenticated user, or tenant. A **backend** stores the
counter. A **decision** says whether the request is allowed and how much budget remains.
The same rule applied to two different users normally has two different counters.
Do not use an unverified user-supplied header as proof of identity.

The application resolves an identity, builds the rules, asks the backend to evaluate
and consume them, and either runs the handler or returns a rejection. Checking first
and incrementing later is unsafe: concurrent requests could all see the last free slot.
UniLimit's `consume` combines evaluation and mutation. `peek` is informational; it does
not reserve capacity.

## Three ways to measure usage

**Fixed window:** count requests during a period starting with that key's first request.
It is simple, but a client can spend one budget just before a boundary and another just
after it.

**Sliding window counter (the default):** blend the previous and current window counts.
This smooths boundary bursts with constant-size state, but estimates recent usage rather
than retaining every timestamp. It is not an exact rolling-window log.

**Token bucket:** refill tokens steadily up to a capacity. Each request spends tokens;
unused capacity permits a burst. Weighted requests can spend more than one token.

Rate limits measure work over time. A **concurrency limit** measures work active right
now. A WebSocket can use a connection lease to cap open connections and a separate rule
to cap messages. Lease expiry recovers capacity after a worker crashes.

## Why Redis?

Memory storage is convenient for a single process. Four workers with independent
10/minute counters can collectively admit 40 requests. Redis gives them shared state.
UniLimit executes rate-limit batches in Redis using Lua, so other requests cannot
interleave inside that script. Memory batches use a lock for the same purpose.

All-or-nothing applies to one batch. A Redis Cluster batch across multiple slots may
partially consume quota. Middleware and route rules are separate batches, so a rejected
route can still spend middleware quota. See the [deployment guide](deployment.md).

## Where the frameworks fit

**Django** provides routing, database models, authentication, templates, and other web
application tools. A *view* is its request handler. *Middleware* wraps the request/response
cycle. UniLimit middleware applies global rules; `@ratelimit` wraps an individual view.
Django session authentication must run before a rule that uses the authenticated user.

**Django REST Framework (DRF)** builds API features on Django: serializers convert and
validate data, API views handle requests, and authentication and permissions determine
who may act. Throttles run after DRF authentication. Use UniLimit's DRF throttles for
JWT/token users; an outer Django decorator cannot see authentication that runs later.

**FastAPI** uses typed Python functions as handlers. A *dependency* is a function or
object FastAPI resolves before running a handler. `Depends(RateLimit(...))` adds a
rate-limit dependency. `setup(app, limiter)` installs middleware and shutdown cleanup.
For authenticated quotas, explicitly connect your authentication dependency as shown in
[the authentication guide](fastapi/authentication.md).

**Starlette** supplies ASGI building blocks such as requests, middleware, and WebSockets;
FastAPI builds on it. **ASGI** supports asynchronous request handling; **WSGI** is the
traditional synchronous interface. `async` code can yield while waiting for Redis,
allowing other requests to progress. It does not make CPU-heavy work automatically fast.

Next: run the [README example](../README.md#try-it-in-two-minutes), then choose a
[framework guide](README.md).
