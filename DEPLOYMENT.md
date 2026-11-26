# Deployment Information — Part 6 Production AI Agent

## Trạng thái hiện tại

- **Mã nguồn Part 6:** [06-lab-complete/](06-lab-complete/), 28/28 kiểm tra tĩnh và 18/18 test đạt. [Kiểm tra tĩnh](evidence/submission/wsl_part6_checker_static.txt) · [pytest](evidence/submission/wsl_part6_pytest.txt).
- **Kiểm chứng local:** Grader trong WSL 2 với Docker và Redis đạt 12/12 tiêu chí, 43/43 kiểm tra. [Kết quả grader](evidence/submission/wsl_part6_checker_full.txt).
- **CP6:** Agent Part 6 đã triển khai trên Render và đạt cả bốn phép thử HTTP công khai.

## Public URL

- **URL:** `https://k5-day14-production-agent-d13t.onrender.com`
- **Kiểm tra nhanh:** [GET /health](https://k5-day14-production-agent-d13t.onrender.com/health) và [GET /ready](https://k5-day14-production-agent-d13t.onrender.com/ready).
- API khai báo `/health`, `/ready` và `/ask`. Truy cập URL gốc `/` trả `404`.

## Nền tảng và cấu hình

Cả hai cấu hình nền tảng trong `06-lab-complete/` đều hợp lệ theo kiểm tra tĩnh của `check_production_ready.py` và dùng **build context = `06-lab-complete/`** (`python:3.11-slim` multi-stage `Dockerfile`, chạy `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2` dưới user non-root `appuser`):

1. **Đã triển khai trên Render** ([render.yaml](06-lab-complete/render.yaml)).
   - Blueprint `lab14-cp6` tạo Web Service `k5-day14-production-agent` và Key Value `k5-day14-agent-redis` tại Singapore, cùng dùng gói Free.
   - Web Service dùng `runtime: docker`, `rootDir: 06-lab-complete`, `dockerfilePath: ./Dockerfile`, `dockerContext: .` và `healthCheckPath: /health`.
   - Khai báo Key Value `k5-day14-agent-redis` và nối `REDIS_URL` qua `fromService`. `noeviction` bảo vệ bộ đếm khi bộ nhớ đầy. Plan free lưu dữ liệu trong bộ nhớ và có thể mất trạng thái khi Key Value khởi động lại. [Render Blueprint YAML Reference](https://render.com/docs/blueprint-spec#render-key-value).
   - Biến bí mật `AGENT_API_KEY` dùng `sync: false` (nhập trực tiếp trên Render Dashboard, không ghi vào Git).
2. **Phương án thay thế — Railway ([railway.toml](06-lab-complete/railway.toml)):**
   - `builder = "DOCKERFILE"`, `dockerfilePath = "Dockerfile"`, `healthcheckPath = "/health"`.
   - Khi cấu hình service Railway, đặt **Root Directory** là `/06-lab-complete` và **Config File** là `/06-lab-complete/railway.toml`. Thêm Redis cùng project, nối `REDIS_URL=${{Redis.REDIS_URL}}` và đặt `AGENT_API_KEY` qua Railway Variables. [Railway Build Configuration](https://docs.railway.com/builds/build-configuration).

## Biến môi trường (chỉ nêu tên và giá trị mẫu không nhạy cảm)

| Biến | Giá trị mẫu / Nguồn | Ghi chú |
| --- | --- | --- |
| `PORT` | `8000` (hoặc platform tự inject) | `Dockerfile` dùng `${PORT:-8000}` |
| `ENVIRONMENT` | `production` | Hỗ trợ `development`, `staging`, `production` |
| `LOG_LEVEL` | `INFO` | Xuất JSON 1 dòng hợp lệ mỗi sự kiện |
| `AGENT_API_KEY` | `<SET_IN_PLATFORM_SECRETS>` | Sinh ngẫu nhiên trước khi đặt (vd `python -c "import secrets; print(secrets.token_urlsafe(32))"`); **tuyệt đối không** ghi vào Git |
| `REDIS_URL` | `<MANAGED_REDIS_URL>` | Trên Render tự nối qua `fromService`; trên Railway nối từ Redis plugin |
| `REDIS_TIMEOUT_SECONDS` | `1.5` | Timeout hữu hạn cho ping/lệnh Redis |
| `RATE_LIMIT_PER_MINUTE` | `10` | Sliding window 60s trong Redis theo hash của API key |
| `DAILY_BUDGET_USD` | `1.0` | Trần chi phí giả lập theo ngày (UTC) |
| `MONTHLY_BUDGET_USD` | `10.0` | Trần chi phí giả lập theo tháng (UTC) |

## Kiểm tra endpoint public (CP6)

| Yêu cầu | Mã kỳ vọng | Mã thực tế |
| --- | ---: | ---: |
| `GET /health` | 200 | 200 |
| `GET /ready` | 200 | 200 |
| `POST /ask` thiếu `X-API-Key` | 401 | 401 |
| `POST /ask` có `X-API-Key` hợp lệ | 200 | 200 |

Kết quả chạy ngày 26/11/2026. [Ảnh kiểm tra](screenshots/test.png)

```bash
export BASE="https://k5-day14-production-agent-d13t.onrender.com"
read -rsp "API key: " AGENT_API_KEY
printf '\n'
export AGENT_API_KEY

# 1) Liveness -> kỳ vọng 200
curl -sS --max-time 120 -o /dev/null -w '%{http_code}\n' "$BASE/health"

# 2) Readiness (đã nối cloud Redis) -> kỳ vọng 200
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/ready"

# 3) POST /ask thiếu X-API-Key -> kỳ vọng 401
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" \
  -H 'Content-Type: application/json' -d '{"question":"Hello"}'

# 4) POST /ask có X-API-Key hợp lệ -> kỳ vọng 200
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" \
  -H 'Content-Type: application/json' -H "X-API-Key: $AGENT_API_KEY" \
  -d '{"question":"Hello"}'

# Sau đó chạy checker kiểm tra public URL:
cd 06-lab-complete && python check_production_ready.py --public-url "$BASE"
```

## Ảnh minh chứng triển khai

- Blueprint đồng bộ và tạo hai dịch vụ. [Dashboard](screenshots/dashboard.png)
- Web Service hiển thị trạng thái **Live** và URL public. [Dịch vụ đang chạy](screenshots/running.png)
- Bốn yêu cầu HTTP trả mã `200 / 200 / 401 / 200`. [Kết quả kiểm tra](screenshots/test.png)
