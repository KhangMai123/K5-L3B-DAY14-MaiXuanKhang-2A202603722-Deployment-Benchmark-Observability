# Benchmark & Observability Report — Day 14

> Báo cáo được tạo từ `06-lab-complete/benchmark-observability/BENCHMARK_REPORT_TEMPLATE.md`.
> Báo cáo dùng số liệu từ JSON benchmark và eval, CSV Locust, API Prometheus, trace Jaeger và ảnh của stack đang chạy trong WSL 2.

- **Họ tên / MSSV:** Mai Xuân Khang · 2A202603722
- **Lớp:** L3B
- **Ngày chạy:** 2026-11-25 và 2026-11-26 (GMT+7)
- **Môi trường:** Benchmark develop và eval chạy bằng Python 3.11.18 trên Windows 11 Home. Benchmark production và observability chạy bằng Docker Compose trên Ubuntu WSL 2. Mock LLM dùng cấu hình nền của bài lab trong các lượt đo tải, gồm `MOCK_LATENCY_MS=120`, `MOCK_SLOW_RATE=0.03` và `MOCK_ERROR_RATE=0.01`.

---

## 1. SLO của agent

| SLI | SLO | Lý do chọn ngưỡng này |
|---|---|---|
| p95 latency `/ask` | **≤ 500 ms** | Khớp ngưỡng mặc định trong `load_test.py` (`--slo-p95-ms 500`) và hợp lý với mock LLM nền ~120–180 ms + retrieval ~20 ms: ở tải vừa (`c ≤ 20`, `slow_rate ≤ 0.03`), 95% người dùng vẫn nhận phản hồi dưới 0.5s trong khi vẫn phát hiện được điểm bão hòa hoặc tail latency vượt ngưỡng. |
| Error rate `/ask` | **≤ 2.0 %** | Khớp `--slo-error-rate 0.02` của `load_test.py`. Tỉ lệ lỗi mock mặc định là `1%` (`MOCK_ERROR_RATE=0.01`), nên ngưỡng `2%` (error budget) cho phép sai số thống kê nhẹ trên 300 request mà không báo động giả, còn ngưỡng alert Prometheus lỏng hơn (`> 5%` trong `1m`) để tránh alert fatigue. |
| Accuracy trên golden dataset | **≥ 85 %** (Gate CI) / **100 %** (Target sau sửa KB) | Theo `eval/run_eval.py` và Exercise 7.3: gate CI chặn merge đặt `--min-accuracy 0.85` (zero regressions so với baseline) để ngăn giảm chất lượng đột ngột, trong khi bộ 23 câu golden hiện có đặt mục tiêu `1.0` (23/23) sau khi bổ sung đủ knowledge base. |

---

## 2. Benchmark hiệu năng (`develop/load_test.py`)

### 2.1 Concurrency vs latency (agent v2)

Chạy tuần tự trên `http://localhost:8002` (`AGENT_VERSION=v2`, `MOCK_SLOW_RATE=0.03`, `MOCK_ERROR_RATE=0.01`, mỗi lượt 300 requests, kiểm tra `/health` trước mỗi lượt). Nguồn: `reports/load_v2_c1.json` → `load_v2_c100.json`, log chi tiết tại [part7_load_tests.txt](evidence/submission/part7_load_tests.txt).

| Concurrency | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate | SLO | Exit code |
|---|---|---|---|---|---|---|---|
| 1 | 4.25 | 202.6 | 233.7 | 1697.7 | 0.67% (`298×200, 2×503`) | ✅ PASS | 0 |
| 10 | 35.31 | 198.8 | 438.4 | 1711.0 | 1.33% (`296×200, 4×503`) | ✅ PASS | 0 |
| 50 | 90.0 | 207.9 | 1666.0 | 1751.1 | 0.67% (`298×200, 2×503`) | ❌ FAIL (`p95 > 500ms`) | 1 |
| 100 | 39.56 | 790.3 | 5017.5 | 5560.4 | 0.67% (`298×200, 2×503`) | ❌ FAIL (`p95 > 500ms`) | 1 |

**Nhận xét:** RPS **không tăng tuyến tính** theo concurrency:
- Từ `c=1` lên `c=10`, RPS tăng ~8.3× (`4.25 → 35.31 req/s`) trong khi `p50` giữ nguyên ~200 ms vì các coroutine `asyncio.sleep` xử lý I/O mock song song tốt; tuy nhiên các request slow 1.5s bắt đầu kéo `p95` lên `438.4 ms`.
- Ở `c=50`, RPS đạt 90,0 req/s nhưng p95 tăng lên 1.666,0 ms. Một số request rơi vào đuôi độ trễ và làm lượt chạy vượt SLO.
- Ở `c=100`, throughput giảm xuống 39,56 req/s cùng lúc p50 tăng lên 790,3 ms và p95 lên 5.017,5 ms. Kết quả cho thấy hệ thống quá tải ở mức concurrency này. Phép đo chưa cô lập nguyên nhân cụ thể.

### 2.2 Tail latency (`--requests 300 --concurrency 20`)

Restart lại agent v2 sạch trước mỗi mức `MOCK_SLOW_RATE` (`0`, `0.03`, `0.1`), giữ nguyên các tham số nền. Nguồn: `reports/load_v2_slow_*.json` và [part7_load_tests.txt](evidence/submission/part7_load_tests.txt#L80-L138).

| `MOCK_SLOW_RATE` | avg (ms) | p95 (ms) | p99 (ms) | RPS | Error rate | SLO |
|---|---|---|---|---|---|---|
| 0 | 209.0 | 430.6 | 493.3 | 92.87 | 1.33% | ✅ PASS |
| 0.03 (mặc định) | 245.0 | 467.8 | 1703.6 | 66.16 | 0.67% | ✅ PASS |
| 0.1 | 382.2 | 1694.0 | 1736.1 | 46.14 | 0.67% | ❌ FAIL |

**Nhận xét:**
- **Chỉ số nhạy nhất với request chậm là `p99` (ở tỷ lệ 3%) và `p95` (khi tỷ lệ vượt 5%, ở mức 10%):** khi `MOCK_SLOW_RATE` tăng từ `0` lên `0.03`, `avg` chỉ nhích nhẹ `+36.0 ms` (`209.0 → 245.0 ms`, +17%), nhưng **`p99` nhảy vọt ~3.45×** từ `493.3 ms` lên `1703.6 ms`. Khi tăng lên `0.1` (10% > 5% số request bị chậm), **`p95` nhảy ~3.6×** từ `467.8 ms` lên `1694.0 ms` làm rớt SLO dù `avg` vẫn mới ở `382.2 ms`.
- **Vì sao SLO không nên đặt theo average:** giá trị trung bình làm phẳng nhóm đuôi (tail), che giấu trải nghiệm tệ hại của 1%–10% người dùng phải chờ gần 2 giây. Đặt SLO theo `p95`/`p99` bảo vệ trải nghiệm số đông lẫn nhóm tail thực tế.

### 2.3 Instrument Prometheus Metric (Exercise 7.1)

Đã thêm Histogram `agent_question_length_chars` với các bucket `(20, 50, 100, 200, 500, 1000, 2000)` quan sát `len(body.question)` trước bước retrieval/LLM vào cả `develop/app.py` và `production/app/{metrics.py,main.py}`. Ảnh chụp `/metrics` thật từ `http://localhost:8002/metrics` lưu tại [part7_metrics.txt](evidence/submission/part7_metrics.txt):
- **Counter:** `http_requests_total{method="POST",route="/ask",status="200"} 2.0`, `llm_requests_total{model="mock-llm-small",status="ok"} 2.0`
- **Gauge:** `agent_in_flight_requests 0.0`, `agent_info{model="mock-llm-small",version="v2"} 1.0`
- **Histogram:** `agent_question_length_chars_bucket{le="50.0"} 2.0`, `agent_question_length_chars_count 2.0`, `agent_question_length_chars_sum 89.0` (tổng đúng 40 + 49 ký tự của 2 request kiểm thử).
- **Lý do dùng `route.path` / `"unmatched"` làm label `route`:** nếu gán URL thật (`request.url.path`), khi bot quét hàng nghìn đường dẫn rác (`/wp-admin`, `/.env`, `/a1b2c3...`), mỗi URL sinh ra một time series mới (high cardinality) khiến Prometheus bùng nổ bộ nhớ RAM và chậm truy vấn. Tương tự, độ dài câu hỏi biến thiên liên tục nên phải dùng Histogram bucket thay vì label `length=...`.

---

## 3. Benchmark chất lượng (`develop/eval/run_eval.py`)

> Để tách biệt lỗi mạng ngẫu nhiên khỏi năng lực trả lời của knowledge base, cả ba lượt eval đều đặt nhất quán `MOCK_ERROR_RATE=0` trên v1 (`8001`) và v2 (`8002`) — không sửa file bất biến `golden_dataset.jsonl`. Ghi nhận trước khi sửa KB tại `reports/eval_v1.json`, `reports/eval_v2.json` ([part7_eval_baseline.txt](evidence/submission/part7_eval_baseline.txt)) và sau khi sửa KB tại `reports/eval_v2_fixed.json` ([part7_eval_fixed.txt](evidence/submission/part7_eval_fixed.txt)).

| Chỉ số | Agent v1 | Agent v2 (Baseline) | Agent v2 (Sau khi sửa KB) |
|---|---|---|---|
| Accuracy | **4.3%** (`1/23`, `0.0435`) | **91.3%** (`21/23`, `0.913`) | **100.0%** (`23/23`, `1.0`) |
| Avg tokens / câu | 57.6 | 109.0 | 114.2 |
| Tổng cost (USD) | \$0.000411 | \$0.000760 | \$0.000795 |
| Cost / câu đúng (USD) | \$0.00041115 | \$0.00003619 | \$0.00003458 |
| Quality gate | ❌ **FAIL** (`--min-accuracy 0.8`) | ✅ **PASS** (`--min-accuracy 0.8`, 0 regression) | ✅ **PASS** (`--min-accuracy 1.0`, 0 regression) |

*(Ghi chú về latency của 23 câu eval: `eval_v1.json` có `p50=172.2ms, p95=201.6ms`; `eval_v2.json` có `p50=217.0ms, p95=1592.9ms`; `eval_v2_fixed.json` có `p50=202.5ms, p95=246.8ms`. Theo đúng cảnh báo trong hướng dẫn lab, mẫu 23 câu quá nhỏ nên 1 câu trúng `MOCK_SLOW_RATE=0.03` làm `p95` dao động mạnh từ `246.8ms` đến `1592.9ms`; **không** dùng con số latency từ eval 23 câu để kết luận hiệu năng chịu tải).*

**Những câu v2 trả lời sai ban đầu (trong `reports/eval_v2.json`) và cách sửa:**

| ID | Keyword bị thiếu (`missing_keywords`) | Cách sửa trong `KNOWLEDGE_BASE` (`develop/utils/mock_llm.py` & đồng bộ `production/utils/mock_llm.py`) |
|---|---|---|
| `security-02` | `token`, `hết hạn`, `chữ ký` | Bổ sung entry `(["jwt"], ...)` giải thích đúng khái niệm: API key là chuỗi cố định không tự hết hạn, còn **JWT (JSON Web Token)** là **token** có **chữ ký** số, chứa thông tin user và thời điểm **hết hạn**, giúp server xác minh mà không cần tra DB. |
| `scaling-03` | `cpu`, `memory`, `custom metrics` | Bổ sung entry `(["hpa", "kubernetes", "autoscal"], ...)` giải thích **Kubernetes HPA (Horizontal Pod Autoscaler)** tự động scale số lượng pod dựa trên **CPU**, **memory** hoặc **custom metrics** (như RPS, độ dài hàng đợi). |

**Nhận xét về trade-off chất lượng / chi phí / latency giữa v1 và v2:**
- **Chất lượng:** v1 không có retrieval nên chỉ trúng 1/23 câu (`4.3%`); v2 bổ sung RAG đạt `91.3%` ban đầu và `100%` (`23/23`, `0 regression`) sau khi cập nhật KB.
- **Chi phí:** prompt của v2 có thêm context nên `avg_tokens` tăng gần gấp đôi (`57.6 → 114.2 tokens/câu`) và tổng cost tăng ~1.93× (`$0.000411 → $0.000795`). Tuy nhiên tính trên hiệu quả thực dụng, **`cost / câu đúng` của v2 (`$0.00003458`) rẻ hơn ~11.9× so với v1 (`$0.00041115`)**.
- **Latency:** v2 tốn thêm bước `search_knowledge_base` (~20 ms) và `input_tokens` dài hơn (tăng nhẹ latency LLM `input_tokens * 0.2ms`, thêm ~30–40 ms ở mức nền `p50` 172 ms → 202 ms), là đánh đổi hoàn toàn xứng đáng để đổi từ `4.3%` lên `100%`.

### Quality Gate & CI Workflow (`.github/workflows/eval.yml`)

- Đã tạo [.github/workflows/eval.yml](.github/workflows/eval.yml) chạy trên `ubuntu-latest` (Python 3.11), cài `develop/requirements.txt`, khởi động `app.py` với `AGENT_VERSION=v2`, `MOCK_ERROR_RATE=0`, `MOCK_SLOW_RATE=0`, poll `/health` có timeout, rồi gọi `python eval/run_eval.py --url http://localhost:8000 --min-accuracy 0.85` và truyền exit code ra khỏi step (kèm `trap cleanup EXIT`).
- **Kiểm chứng cục bộ ([part7_ci_local_check.txt](evidence/submission/part7_ci_local_check.txt)):**
  - Lệnh CI trên v2 đạt **23/23 (100.0%)**, `EXIT_CODE: 0` (`✅ PASS`).
  - Lệnh negative gate đối chứng (chạy v1 so với `--baseline reports/eval_v2.json --min-accuracy 0.85`, lưu tại [eval_v1_gate_fail.json](evidence/submission/eval_v1_gate_fail.json)) cho kết quả `4.3%`, **`20 regressions`**, **`EXIT_CODE: 1`** (`❌ FAIL`).
- Workflow dùng ngưỡng 85% và so với `reports/eval_v2_fixed.json`. Job trả mã lỗi nếu dưới ngưỡng hoặc một câu đúng trong baseline bị regression. Đặt job `eval-quality-gate` làm required status check để áp dụng điều kiện khi merge.

---

## 4. Observability stack (`production/`)

Docker Compose chạy agent, Nginx, Prometheus, Grafana, Jaeger và Locust trong WSL 2. `promtool test rules /p/alert_rules_test.yml` trả `SUCCESS`. Production chạy bằng `/opt/venv` dưới UID 999 với một worker mỗi container. Histogram `agent_question_length_chars` có dữ liệu tại `/metrics` nội bộ. [Kết quả promtool](evidence/submission/wsl_part7_promtool.txt).

### 4.1 Ảnh minh chứng

- [Grafana khi Locust đang tạo tải](screenshots/grafana-dashboard.png). Ảnh cho thấy hai replica hoạt động với khoảng 17,9 req/s, tỷ lệ lỗi 1,61% và p95 khoảng 292 ms.
- [Jaeger, trace chậm](screenshots/jaeger-slow-trace.png) và [Jaeger, trace HTTP 503](screenshots/jaeger-error-trace.png).
- [Prometheus, hai alert cùng firing](screenshots/prometheus-alert-firing.png). Ảnh hiển thị cả `AgentHighErrorRate` và `AgentHighLatencyP95` đang firing.

### 4.2 Thử nghiệm lỗi giả lập và alert

Thay đổi biến môi trường qua Compose và tạo tải cục bộ qua Nginx. Đo thời gian từ lúc bắt đầu áp dụng cấu hình, hoặc từ lúc pause với thí nghiệm D, đến lần đọc API Prometheus đầu tiên thấy trạng thái `firing`. A và B dùng chung một lượt để quan sát hai alert đồng thời.

| Thử nghiệm | Thay đổi | Kết quả quan sát | Panel Grafana | Bằng chứng |
|---|---|---|---|---|
| A | `MOCK_ERROR_RATE=0.2` | `AgentHighErrorRate` firing sau **81.2 s** | `Error rate` | [Prometheus A+B](evidence/submission/wsl_part7_alert_ab.json) |
| B | `MOCK_SLOW_RATE=0.3` | `AgentHighLatencyP95` firing sau **141.2 s**, cùng lúc alert A còn firing | `Latency p95` | [Prometheus A+B](evidence/submission/wsl_part7_alert_ab.json) |
| C | `LLM_MODEL=mock-llm-large` | `LLMCostBurnRateHigh` firing sau **155.1 s**, ở mức **17.61 USD/giờ** theo mô hình chi phí giả lập | `Cost burn rate (USD/giờ)` | [Prometheus C](evidence/submission/wsl_part7_alert_c.json) |
| D | Pause một trong hai replica | `AgentInstanceDown` firing sau **40.2 s**. Gateway trả **4/16 HTTP 504**, còn lại 12 HTTP 200 | `Instances up` | [Prometheus D](evidence/submission/wsl_part7_alert_d.json) · [Mã HTTP](evidence/submission/wsl_part7_gateway_d.json) |
| E | `AGENT_VERSION=v1` | Sau 30 s, API Prometheus ghi nhận 2 replica v1 và danh sách alert active rỗng. Eval cố định cho thấy v1 đạt 1/23, v2 sau sửa đạt 23/23 | `Phiên bản đang chạy` | [Trạng thái alert](evidence/submission/wsl_part7_alert_e.json) · [Metric phiên bản](evidence/submission/wsl_part7_v1_metric.json) |

Pause giữ bản ghi DNS của replica trong lúc không phản hồi scrape, nên Prometheus còn mục tiêu để đánh dấu `up = 0`. HTTP 504 phát sinh ở Nginx khi quá thời gian chờ upstream. Cần theo dõi HTTP code ngay tại gateway để thấy lỗi này. Thử nghiệm E cho thấy cần kết hợp eval trên golden dataset và tín hiệu chất lượng vì các rule hạ tầng hiện tại theo dõi lỗi HTTP, độ trễ, tình trạng instance và chi phí.

### 4.3 Phân tích trace (Exercise 7.5) & Trả lời câu hỏi Exercise 7.4 / Cuối README Part 7

- Trace chậm `1defdadda3392bf189c8db3e4334645d` có thời gian toàn tuyến **1684.46 ms**. Span `chat mock-llm-small` chiếm **1664.17 ms (98.8%)**. Trace lỗi `96a8cc27e3fbc9e23369cbc28cbd846c` trả HTTP 503, kéo dài **857.62 ms** và có event `exception` trên span LLM. [Trace chậm](evidence/submission/wsl_part7_jaeger_slow.json) · [Trace lỗi](evidence/submission/wsl_part7_jaeger_error.json) · [Log đối chiếu](evidence/submission/wsl_part7_trace_logs.jsonl).

Dòng log ứng dụng có cùng `trace_id`:

```json
{"ts": "2026-11-26T02:19:18.911090+00:00", "level": "INFO", "service": "observable-agent", "instance": "09369595e64d", "event": "llm_call", "trace_id": "1defdadda3392bf189c8db3e4334645d", "span_id": "32991c26e0919b79", "request_id": "0c40baecb178", "model": "mock-llm-small", "agent_version": "v2", "input_tokens": 79, "output_tokens": 46, "cost_usd": 3.9e-05, "llm_ms": 1664.5}
```
- **Phân tích mã nguồn `production/app/main.py` & `telemetry.py` (Exercise 7.4 & 7.5):**
  1. **Exercise 7.4 (p95 vs avg, traffic 2 instance, input vs output tokens, `/metrics` qua Nginx):**
     - Chạy 30 người dùng, tăng 5 người dùng/giây. Tại lần đọc Prometheus khi đang có tải, p95 **291.8 ms**, trung bình **227.2 ms**, lỗi **1.54%**. p95 cao hơn trung bình nên cần đọc cùng p99 để thấy độ trễ của nhóm request chậm. [Số đo Prometheus](evidence/submission/wsl_part7_dashboard_metrics.json).
     - Nginx chia tải gần đều trong cửa sổ đo: `172.18.0.4:8000` xử lý **9.44 req/s**, `172.18.0.5:8000` xử lý **9.31 req/s**. [Số đo Prometheus](evidence/submission/wsl_part7_dashboard_metrics.json).
     - Input đạt **1431.6 token/s**, output **749.3 token/s**. Input của v2 bao gồm system prompt, câu hỏi và tài liệu retrieval. Output chủ yếu là câu trả lời mock ngắn.
     - Gọi `GET /metrics` qua Nginx trả **404**. Rule `location = /metrics` giới hạn truy cập từ client, còn Prometheus scrape trực tiếp agent trên mạng Compose. [Kết quả HTTP](evidence/submission/wsl_part7_dashboard_metrics.json).
  2. **Các câu hỏi thảo luận cuối README Part 7:**
     - **Câu 1 (Error/latency xanh nhưng user báo trả lời sai):** Observability hạ tầng không phát hiện được; cần eval CI trên golden dataset, online eval (LLM-as-judge), theo dõi retrieval hit ratio và user feedback.
     - **Câu 2 (Prometheus pull model vs Cloud Run scale-to-zero):** Pull hợp với service chạy thường trực (Kubernetes/Docker DNS discovery, đơn giản, không cần client biết địa chỉ collector và Prometheus tự biết target chết khi scrape lỗi → `up == 0`). Với serverless scale-to-zero như Cloud Run/Lambda, container có thể bị tắt hoặc đổi IP trước chu kỳ scrape nên mô hình pull không còn phù hợp; cần chuyển sang **push** (OTLP metrics, Google Cloud Monitoring, Prometheus Remote Write hoặc Pushgateway cho batch job ngắn).
     - **Câu 3 (Thêm label `user_id` với 100.000 user vào `http_requests_total`):** Tạo ra bùng nổ cardinality (`100,000 × methods × routes × statuses` = hàng triệu time series), làm Prometheus ngốn RAM, tăng thời gian đánh giá PromQL/compaction và có thể OOM crash. Giải pháp: giữ label low-cardinality ở metrics, đưa `user_id` vào **logs** hoặc **trace attributes**.
     - **Câu 4 (Trace mọi request vs Sampling 10%):** Tracing 100% ở tải lớn gây tốn CPU/RAM serialize span, băng thông mạng OTLP và chi phí lưu trữ Jaeger. Sampling 10% giảm ~90% overhead nhưng có rủi ro bỏ sót các lỗi hiếm gặp (`<1%`) hoặc các tail-latency trace nếu dùng head-based random sampling; thực tế hay dùng **tail-based sampling** (giữ 100% trace lỗi/chậm và sample 5–10% trace bình thường).
     - **Câu 5 (`for: 0s` vs `for: 30m` trên alert error rate):** `for: 0s` gây **alert fatigue** vì chỉ một spike ngắn hoặc 1–2 request lỗi trong cửa sổ đầu cũng kích hoạt cảnh báo ngay. Ngược lại `for: 30m` khiến sự cố nghiêm trọng (error rate > 5%) kéo dài suốt nửa tiếng mới báo, vi phạm nặng error budget trước khi kỹ sư kịp xử lý.

---

## 5. Benchmark scaling (`production/`)

Chạy Locust headless với 100 người dùng, tăng 10 người dùng/giây, mỗi cấu hình chạy 2 phút. Đo trên dòng `POST /ask` của CSV gốc. Cả hai lượt Locust trả exit `1` do có phản hồi 503 từ cơ chế lỗi giả lập mặc định. CSV lưu đầy đủ lần gọi thành công lẫn thất bại. [Log hai lượt chạy](evidence/submission/wsl_part7_benchmark.txt).

| Replica | RPS (`/ask`) | p50 (ms) | p95 (ms) | p99 (ms) | Thất bại | Dữ liệu |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 59.89 | 190 | 230 | 1700 | 73/7151 (1.02%) | [CSV](benchmark-results/scale_1_stats.csv) |
| 3 | 60.53 | 190 | 220 | 1700 | 63/7226 (0.87%) | [CSV](benchmark-results/scale_3_stats.csv) |

Tăng từ 1 lên 3 replica làm `/ask` tăng từ 59.89 lên 60.53 req/s trong cấu hình này. p95 giảm từ 230 xuống 220 ms và p99 vẫn 1700 ms. Mức tải và nhịp nghỉ của mỗi người dùng giới hạn số request gửi tới hệ thống. Độ trễ của LLM mock giải thích phần lớn thời gian của trace chậm, nên tăng replica ít tác động lên đuôi độ trễ được quan sát.

## 6. Kết luận

1. **3 điều rút ra trực tiếp từ các phép đo thực tế trong lab:**
   - **Latency trung bình che giấu tail latency và điểm bão hòa:** Ở `concurrency=20`, tăng `MOCK_SLOW_RATE` từ `0` lên `0.03` chỉ làm `avg` tăng `+36ms` (`209.0 → 245.0ms`) nhưng **`p99` vọt 3.45× (`493.3 → 1703.6ms`)**; tăng concurrency từ `50` lên `100` làm throughput đảo chiều tụt từ `90.0` xuống `39.56 req/s` và `p95` vọt lên `5017.5ms`.
   - **Đánh giá AI agent phải kết hợp accuracy + `cost / câu đúng`, không thể dựa vào HTTP 200:** v2 tốn gần gấp đôi token mỗi câu (`109.0–114.2` vs `57.6`) nhưng tăng accuracy từ `4.3%` lên `91.3% → 100%`, giúp **`cost / câu đúng` rẻ hơn ~11.9×** (`$0.00003458` vs `$0.00041115`).
   - **Quality gate tự động dựa trên exit code bắt được regression ngay trước khi merge:** `run_eval.py` với `--min-accuracy` và `--baseline` trả exit code `0` khi đạt `23/23` (`0 regressions`) và chặn ngay bản v1 bằng exit code `1` (`20 regressions`).
2. **Metric và alert nên bổ sung nếu đưa agent này lên production thật:**
   - **Metrics & Alerts nên thêm:**
     - `agent_cost_budget_utilization_ratio` (Gauge % budget đã tiêu trong ngày/tháng) + alert cảnh báo sớm ở ngưỡng 80% và 95% (kết hợp Redis cost guard của Part 6).
     - `agent_rate_limit_rejections_total{reason="429"}` để phát hiện tấn công brute-force hoặc khách hàng vượt quota bất thường.
     - `agent_redis_operation_duration_seconds` & alert `RedisDown` cho các thành phần phụ thuộc trạng thái.
     - **Synthetic blackbox/Nginx metrics** (tỉ lệ `4xx/504` tại reverse proxy) để không bỏ sót lỗi `504` khi agent treo như trường hợp thí nghiệm D.
     - **Quality monitoring online** (retrieval miss rate spike, user feedback score, hoặc eval định kỳ trên mẫu log sản xuất) vì metric hạ tầng không phát hiện được lỗi chất lượng như thí nghiệm E.
