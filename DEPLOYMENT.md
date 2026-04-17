# Deployment Information

## Public URL

https://vinai-production.up.railway.app

## Platform

Railway

## Test Commands

### Health Check
```bash
curl https://vinai-production.up.railway.app/health
# Expected: {"status":"ok","uptime_seconds":...,"version":"1.0.0","environment":"production",...}
```

### Readiness Check
```bash
curl https://vinai-production.up.railway.app/ready
# Expected: {"ready":true,"in_flight_requests":0}
```

### Authentication Required (no key → 401)
```bash
curl https://vinai-production.up.railway.app/ask
# Expected: {"detail":"Missing API key. Include header: X-API-Key: <your-key>"}
```

### API Test (with authentication)
```bash
curl -X POST https://vinai-production.up.railway.app/ask \
  -H "X-API-Key: cognaig" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test", "question": "Hello"}'
# Expected: {"answer":"..."}
```

### Rate Limiting Test (should return 429 after 10 req/min)
```bash
for i in {1..15}; do
  curl -s -X POST https://vinai-production.up.railway.app/ask \
    -H "X-API-Key: cognaig" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "test", "question": "test '$i'"}' | python3 -m json.tool
  echo "---"
done
# Requests 1-10: 200 OK
# Requests 11+:  429 Too Many Requests
```

## Environment Variables Set

| Variable | Value |
|---|---|
| `REDIS_URL` | redis://localhost:6379/0 |
| `AGENT_API_KEY` | cognaig |
| `ENVIRONMENT` | `production` |
| `RATE_LIMIT_PER_MINUTE` | `20` |
| `DAILY_BUDGET_USD` | `5.0` |

## Railway Configuration (`railway.toml`)

```toml
[build]
builder = "DOCKERFILE"

[deploy]
startCommand = "sh -c 'uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2'"
healthcheckPath = "/health"
healthcheckTimeout = 30
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

## Screenshots

- [Deployment dashboard](screenshots/service_status.png)
- [Service running](screenshots/service_status.png)
- [Test results](screenshots/result.png)
