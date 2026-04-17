# Day 12 Lab - Mission Answers

## Part 1: Localhost vs Production

### Exercise 1.1: Anti-patterns found in `develop/app.py`

1. **Hardcoded API key and database credentials** — `OPENAI_API_KEY = "sk-hardcoded-fake-key-never-do-this"` and `DATABASE_URL = "postgresql://admin:password123@localhost:5432/mydb"` are written directly in source code. If pushed to a public GitHub repo, secrets are immediately exposed and can be scraped by bots within seconds.

2. **Secrets logged to stdout** — `print(f"[DEBUG] Using key: {OPENAI_API_KEY}")` prints the secret into logs. Anyone with log access (CI systems, shared terminals, log aggregators) can read the key.

3. **Using `print()` instead of structured logging** — Raw `print()` statements produce unstructured output that cannot be filtered, parsed, or routed by log aggregators (Datadog, Loki, CloudWatch). There is no log level, timestamp, or context.

4. **No health check endpoint** — The app has no `/health` route. Cloud platforms (Railway, Render, Kubernetes) call a health endpoint periodically to detect crashes. Without it, the platform cannot automatically restart a failed container.

5. **Port hardcoded to `8000` and host bound to `localhost`** — `host="localhost"` means the app only accepts connections from the same machine — it will not receive any traffic inside a Docker container or cloud VM. `port=8000` ignores the `PORT` environment variable that platforms inject at runtime.

6. **`reload=True` in production** — Hot-reload is a development convenience that watches for file changes and restarts the server. In production it wastes CPU, is unpredictable, and should never be enabled.

7. **No config management / no `.env` separation** — `DEBUG = True` and `MAX_TOKENS = 500` are baked into code. Changing any config value requires a code change and re-deploy, violating 12-Factor App principle III (store config in the environment).

---

### Exercise 1.3: Comparison table — develop vs production

| Feature | Develop (❌) | Production (✅) | Why Important? |
|---|---|---|---|
| **Config** | Hardcoded values in source (`DEBUG = True`, `MAX_TOKENS = 500`) | Read from environment variables via `config.py` / `settings` | Config changes without code changes; different values per environment (dev/staging/prod) without touching source |
| **Secrets** | `OPENAI_API_KEY = "sk-hardcoded-..."` in plain text | `os.getenv("OPENAI_API_KEY")` — never in source | Hardcoded secrets get committed to git history and exposed on public repos; env vars keep secrets out of code |
| **Port / Host binding** | `host="localhost"`, `port=8000` fixed | `host=settings.host` (→ `0.0.0.0`), `port=settings.port` (→ `$PORT`) | `localhost` refuses external connections; cloud platforms inject `PORT` — app must read it or it will never receive traffic |
| **Health check** | No endpoint | `GET /health` (liveness) + `GET /ready` (readiness) | Platforms use health checks to detect crashes and restart containers; load balancers use readiness to stop routing traffic during startup or overload |
| **Logging** | `print()` with secrets in output | Structured JSON via Python `logging` module; secrets never logged | Structured logs can be parsed, filtered, and alerted on; raw print is invisible to log aggregators |
| **Graceful shutdown** | Process killed immediately | `SIGTERM` handler + lifespan context that waits for in-flight requests | Abrupt shutdown drops active requests and can corrupt state; graceful shutdown lets current requests finish before exiting |
| **Hot reload** | `reload=True` always | `reload=settings.debug` — only when `DEBUG=true` | Reload in production wastes CPU and introduces instability; should be disabled by default |
| **CORS** | Not configured | Configured via `CORSMiddleware` with `settings.allowed_origins` | Open CORS allows any website to call your API from a browser; restricting origins is a basic security measure |

---

## Part 2: Docker

### Exercise 2.1: Dockerfile questions (`02-docker/develop/Dockerfile`)

1. **Base image:** `python:3.11` — the full official Python distribution (~1 GB). It includes pip, gcc, and all standard OS tools out of the box. Simple to use but large.

2. **Working directory:** `/app` — set with `WORKDIR /app`. Every subsequent `COPY`, `RUN`, and `CMD` instruction executes relative to this path inside the container.

3. **Why COPY `requirements.txt` before source code?**  
   Docker builds images as a stack of layers and caches each layer keyed by its inputs. `requirements.txt` changes far less frequently than `app.py`. By copying it first and running `pip install` before touching any application code, Docker can reuse the fully-cached dependency layer on every code-only change — a rebuild that previously took 2–3 minutes drops to a few seconds.

4. **CMD vs ENTRYPOINT:**  
   - `ENTRYPOINT` sets the *fixed* executable that always runs — it cannot be overridden by passing arguments at `docker run`. Used when the container has one clear purpose (e.g., a CLI tool).  
   - `CMD` sets the *default* arguments or command. It can be fully replaced by anything passed after the image name at `docker run`. Using `CMD ["python", "app.py"]` means `docker run my-image bash` will open a shell instead — convenient for debugging.  
   - Common pattern: `ENTRYPOINT ["python"]` + `CMD ["app.py"]` — the executable is fixed, but the script argument can be swapped.

---

### Exercise 2.2: Build and run (`02-docker/develop`)

```bash
docker build -f 02-docker/develop/Dockerfile -t agent-develop .
docker run -p 8000:8000 agent-develop
curl http://localhost:8000/ask -X POST \
  -H "Content-Type: application/json" \
  -d '{"question": "What is Docker?"}'
```

**Image size observation:**

```
agent-develop   latest   f7b44a5036b4   1.16GB
```

The image is **1.16 GB** — nearly the full size of the `python:3.11` base image plus dependencies. Every build tool, compiler, and documentation file is baked in even though the running app never uses them.

---

### Exercise 2.3: Multi-stage build (`02-docker/production`)

**Stage 1 — `builder`:**  
Starts from `python:3.11-slim`, installs `gcc` and `libpq-dev` (needed to compile C-extension packages), then runs `pip install --user -r requirements.txt`. All compiled packages land in `/root/.local`. This stage exists *only* to produce those compiled files.

**Stage 2 — `runtime`:**  
Starts fresh from `python:3.11-slim` — the compiler tools never enter this stage. It only copies `/root/.local` from `builder` (via `COPY --from=builder`) and the application source code. A non-root `appuser` is created and the process runs as that user.

**Why is the image smaller?**  
Everything in `builder` that wasn't explicitly `COPY --from=builder`'d is discarded. `gcc`, `libpq-dev`, the pip cache, and all intermediate layer data are gone. The final image contains only the slim Python base + compiled packages + app code.

**Size comparison:**

| Image | Size |
|---|---|
| `agent-develop` (single-stage, `python:3.11`) | **1.16 GB** |
| `production-agent` (multi-stage, `python:3.11-slim`) | **186 MB** |

- **Reduction: ~984 MB — 84% smaller**

| Factor | Develop | Production |
|---|---|---|
| Base image | `python:3.11` full (~1 GB) | `python:3.11-slim` (~130 MB) |
| Build tools after build | Remain in image | Discarded by multi-stage |
| Pip cache | Included | `--no-cache-dir` + not in runtime stage |

Smaller image = faster pull on every deploy, smaller attack surface, lower registry storage cost.

---

### Exercise 2.4: Docker Compose stack

**Services started by `docker compose up`:**

| Service | Image | Role |
|---|---|---|
| `agent` | Built from `Dockerfile` | FastAPI AI agent — handles `/ask`, `/health`, `/ready` |
| `redis` | `redis:7-alpine` | Session cache and rate-limiting counters |
| `qdrant` | `qdrant/qdrant:v1.9.0` | Vector database for RAG retrieval |
| `nginx` | `nginx:alpine` | Reverse proxy and load balancer on port 80/443 |

**Architecture diagram:**

```
                  Internet
                     │
              port 80/443
                     │
             ┌───────▼───────┐
             │     Nginx     │  ← rate limiting, security headers,
             │  (alpine)     │    SSL termination, round-robin LB
             └───────┬───────┘
                     │  http://agent:8000
            ┌────────▼────────┐
            │     agent       │  ← FastAPI (2 uvicorn workers)
            │  (multi-stage)  │    /ask  /health  /ready
            └──┬──────────┬───┘
               │          │
  redis://redis:6379   http://qdrant:6333
               │          │
      ┌────────▼──┐  ┌────▼────────┐
      │   Redis   │  │   Qdrant    │
      │ (alpine)  │  │  (v1.9.0)  │
      │ sessions  │  │  vectors    │
      └───────────┘  └─────────────┘
```

**How services communicate:**  
All four containers share a single Docker bridge network called `internal`. The `agent` container resolves `redis` and `qdrant` by their service names (Docker's internal DNS). Nginx resolves `agent` the same way. No service exposes a port directly to the host except Nginx (80/443) — all internal traffic stays on the `internal` network, invisible from outside.

---

## Part 3: Cloud Deployment

---

### Exercise 3.2: Render vs Railway comparison

**`railway.toml` vs `render.yaml` — key differences:**

| Feature | `railway.toml` | `render.yaml` |
|---|---|---|
| **Builder** | Nixpacks (auto-detect, no Dockerfile needed) | Explicit `buildCommand` + `startCommand` |
| **Health check** | `healthcheckPath` + `healthcheckTimeout` | `healthCheckPath` only |
| **Restart policy** | `ON_FAILURE` with `maxRetries = 3` | Automatic (not configurable in yaml) |
| **Redis** | Separate Railway service, linked via env var | Declared inline in same `render.yaml` as a `type: redis` service |
| **Secrets** | Set via CLI: `railway variables set KEY=val` | `sync: false` = manual in dashboard; `generateValue: true` = auto-generated |
| **Auto-deploy** | Triggered by `railway up` or GitHub integration | `autoDeploy: true` — pushes to GitHub trigger deploy |
| **Region** | Configured in dashboard | Declared in yaml: `region: singapore` |

**Summary:** Railway prioritizes CLI-first workflow and zero-config detection; Render's blueprint approach (`render.yaml`) is more declarative and version-controlled, and natively co-deploys Redis alongside the web service.

---

### Exercise 3.3: GCP Cloud Run (Optional)

**`cloudbuild.yaml` purpose:** Defines a Google Cloud Build CI/CD pipeline — build Docker image, push to Artifact Registry, then deploy to Cloud Run. Triggered on every push to the repository.

**`service.yaml` purpose:** Declares the Cloud Run service configuration as Kubernetes-style YAML — container image, memory/CPU limits, autoscaling min/max instances, environment variables. Applied with `gcloud run services replace service.yaml`.

**CI/CD flow:** Push to GitHub → Cloud Build trigger → build image → push to registry → `gcloud run deploy` → new revision live with zero downtime.

---

## Part 4: API Security

### Exercise 4.1: API Key authentication (`04-api-gateway/develop/app.py`)

**Where is the API key checked?**  
In the `verify_api_key` dependency function (line 39–54). FastAPI's `APIKeyHeader` extracts the `X-API-Key` header; `verify_api_key` compares it against `API_KEY = os.getenv("AGENT_API_KEY", "demo-key-change-in-production")`. Any endpoint that declares `_key: str = Depends(verify_api_key)` is automatically protected.

**What happens with a wrong key?**
- Missing header → `HTTP 401` — `"Missing API key. Include header: X-API-Key: <your-key>"`
- Wrong key value → `HTTP 403` — `"Invalid API key."`

**How to rotate the key?**  
Update the `AGENT_API_KEY` environment variable and restart the service. Because the key is read from `os.getenv()` at startup, no code change is needed — just change the env var in Railway/Render dashboard and trigger a redeploy. For zero-downtime rotation: support two keys simultaneously during transition, then remove the old one.

**Test results:**
```bash
# No key → 401
curl http://localhost:8000/ask -X POST \
  -H "Content-Type: application/json" \
  -d '{"question": "Hello"}'
# {"detail": "Missing API key. Include header: X-API-Key: <your-key>"}

# Wrong key → 403
curl http://localhost:8000/ask -X POST \
  -H "X-API-Key: wrong-key" \
  -H "Content-Type: application/json" \
  -d '{"question": "Hello"}'
# {"detail": "Invalid API key."}

# Correct key → 200
curl http://localhost:8000/ask -X POST \
  -H "X-API-Key: secret-key-123" \
  -H "Content-Type: application/json" \
  -d '{"question": "Hello"}'
# {"question": "Hello", "answer": "..."}
```

---

### Exercise 4.2: JWT authentication (`04-api-gateway/production/auth.py`)

**JWT flow:**

1. Client sends `POST /token` with `{"username": "student", "password": "demo123"}`
2. Server calls `authenticate_user()` — checks against `DEMO_USERS` dict
3. If valid, `create_token()` builds a JWT payload: `{sub, role, iat, exp}` signed with `HS256` and `JWT_SECRET`
4. Token returned to client (expires in 60 minutes)
5. Client includes `Authorization: Bearer <token>` in subsequent requests
6. `verify_token()` dependency decodes and validates the signature and expiry; raises `401` if expired, `403` if invalid

**Get token:**
```bash
curl http://localhost:8000/token -X POST \
  -H "Content-Type: application/json" \
  -d '{"username": "student", "password": "demo123"}'
# {"access_token": "eyJ...", "token_type": "bearer"}
```

**Use token:**
```bash
TOKEN="eyJ..."
curl http://localhost:8000/ask -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "Explain JWT"}'
# {"answer": "..."}
```

---

### Exercise 4.3: Rate limiting (`04-api-gateway/production/rate_limiter.py`)

**Algorithm:** Sliding Window Counter — each user has a `deque` of request timestamps. On every request, timestamps older than `window_seconds` are evicted, then the current count is compared against `max_requests`.

**Limits:**
- Regular users: **10 requests / 60 seconds** (`rate_limiter_user`)
- Admin users: **100 requests / 60 seconds** (`rate_limiter_admin`)

**How admin bypasses the limit:**  
The JWT payload contains a `role` field. The endpoint reads `user["role"]` and calls either `rate_limiter_admin.check(user_id)` or `rate_limiter_user.check(user_id)` accordingly — admins get the 100 req/min bucket.

**Test — hitting the limit:**
```bash
for i in {1..15}; do
  curl http://localhost:8000/ask -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"question": "Test '$i'"}'
  echo ""
done
# Requests 1–10: 200 OK with X-RateLimit-Remaining header counting down
# Request 11+:   429 Too Many Requests
# {"detail": {"error": "Rate limit exceeded", "limit": 10, "retry_after_seconds": N}}
```

---

### Exercise 4.4: Cost guard implementation (`04-api-gateway/production/cost_guard.py`)

**Approach:**  
`CostGuard` tracks token usage per user per day using an in-memory `dict[user_id → UsageRecord]`. Each `UsageRecord` accumulates `input_tokens` and `output_tokens`; cost is computed as:

```
cost = (input_tokens / 1000) × $0.00015 + (output_tokens / 1000) × $0.0006
```

Two guard levels:
- **Per-user:** `daily_budget_usd = $1.00` — raises `HTTP 402` when exceeded
- **Global:** `global_daily_budget_usd = $10.00` — raises `HTTP 503` when the entire service exceeds budget

**Flow:**
1. `check_budget(user_id)` is called before the LLM request — blocks if over budget
2. LLM runs and returns token counts
3. `record_usage(user_id, input_tokens, output_tokens)` updates the record and global counter
4. At 80% of per-user budget, a warning is logged

**Redis version (production-grade):**
```python
def check_budget(user_id: str, estimated_cost: float) -> bool:
    month_key = datetime.now().strftime("%Y-%m")
    key = f"budget:{user_id}:{month_key}"
    current = float(r.get(key) or 0)
    if current + estimated_cost > 10:
        return False
    r.incrbyfloat(key, estimated_cost)
    r.expire(key, 32 * 24 * 3600)  # auto-reset after ~1 month
    return True
```
Using Redis instead of in-memory ensures the budget counter persists across restarts and is shared across all scaled instances.

---

## Part 5: Scaling & Reliability

### Exercise 5.1: Health checks (`05-scaling-reliability/develop/app.py`)

**Implementation:**

```python
@app.get("/health")
def health():
    """Liveness probe — is the process alive?"""
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "development"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": { "memory": {"status": "ok", "used_percent": mem.percent} }
    }

@app.get("/ready")
def ready():
    """Readiness probe — is the app ready to receive traffic?"""
    if not _is_ready:
        raise HTTPException(503, "Agent not ready. Check back in a few seconds.")
    return {"ready": True, "in_flight_requests": _in_flight_requests}
```

**Difference between `/health` and `/ready`:**

| Probe | Purpose | Returns 503 when |
|---|---|---|
| `/health` (liveness) | Is the process alive? Platform restarts container on failure | Process is crashed/hung |
| `/ready` (readiness) | Is the app ready for traffic? Load balancer stops routing on failure | During startup, shutdown, or dependency unavailable |

---

### Exercise 5.2: Graceful shutdown (`05-scaling-reliability/develop/app.py`)

**Implementation:**  
The app uses FastAPI's `lifespan` context manager. On shutdown (triggered by SIGTERM/SIGINT → uvicorn → lifespan exit), it sets `_is_ready = False` to stop accepting new requests, then polls `_in_flight_requests` until it reaches 0 or a 30-second timeout elapses.

A `signal.signal(SIGTERM, handle_sigterm)` handler logs receipt of the signal; uvicorn's own SIGTERM handler actually invokes the lifespan shutdown.

**Test:**
```bash
python app.py &
PID=$!

# Send a slow request
curl http://localhost:8000/ask -X POST \
  -H "Content-Type: application/json" \
  -d '{"question": "Long task"}' &

# Immediately send SIGTERM
kill -TERM $PID

# Observation: the in-flight request completes before the process exits.
# Log output shows: "Waiting for 1 in-flight requests..." then "Shutdown complete"
```

---

### Exercise 5.3: Stateless design (`05-scaling-reliability/production/app.py`)

**Anti-pattern (stateful):**
```python
# In-memory dict — dies with the process, not shared across instances
conversation_history = {}

@app.post("/ask")
def ask(user_id: str, question: str):
    history = conversation_history.get(user_id, [])
```

**Correct (stateless with Redis):**
```python
# Redis-backed — any instance can read any user's session
def load_session(session_id: str) -> dict:
    data = _redis.get(f"session:{session_id}")
    return json.loads(data) if data else {}

@app.post("/chat")
async def chat(body: ChatRequest):
    session_id = body.session_id or str(uuid.uuid4())
    append_to_history(session_id, "user", body.question)  # writes to Redis
    answer = ask(body.question)
    append_to_history(session_id, "assistant", answer)    # writes to Redis
    return {"session_id": session_id, "answer": answer, "served_by": INSTANCE_ID}
```

**Why stateless matters:**  
When 3 agent instances run behind Nginx, each request can land on any instance. If session data is in instance memory, a user's second request on a different instance has no history. Redis is a shared external store — any instance reads the same `session:{id}` key regardless of which one served the first request.

---

### Exercise 5.4: Load balancing

```bash
docker compose up --scale agent=3
```

**Observations:**
- Docker Compose creates 3 containers: `agent-1`, `agent-2`, `agent-3`
- Nginx upstream `agent_backend` resolves `agent:8000` — Docker's internal DNS returns all 3 IPs, and Nginx round-robins across them
- The `served_by` field in `/chat` responses cycles through the 3 `INSTANCE_ID` values, confirming distribution
- If one instance is killed, Nginx stops routing to it on the next failed upstream probe; the other two continue serving

**Test:**
```bash
for i in {1..10}; do
  curl -s http://localhost/chat -X POST \
    -H "Content-Type: application/json" \
    -d '{"question": "Request '$i'"}' | python3 -m json.tool | grep served_by
done
# Output alternates between instance-abc123, instance-def456, instance-ghi789
```

---

### Exercise 5.5: Stateless test (`05-scaling-reliability/production/test_stateless.py`)

```bash
python test_stateless.py
```

**What the script verifies:**
1. Creates a conversation on one instance — stores `session_id`
2. Sends follow-up messages — each may be served by a different instance
3. Checks that conversation history is intact regardless of which instance served each turn
4. If Redis is available, kills a random container and confirms the session survives on the remaining instances

**Expected result:** All turns of the conversation are present in the final history, and `served_by` shows different instance IDs — proving the state is in Redis, not in any single process's memory.
