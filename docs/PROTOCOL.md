# 上游协议规格（WorkBuddy / CodeBuddy ⇄ 网关）

本文是这个仓库的**协议事实清单**：上游要求什么、拒绝什么、错误码意味着什么。内容整理自两个参考实现的源码及其注释里的实测结论；本项目的 `wb2api/identity.py`、`wb2api/adapters/chat.py`、`wb2api/service.py`、`wb2api/sanitize.py` 逐条实现了这里的东西。

建议阅读顺序：§0 传输 → §1 请求头 → §2 请求体改写 → §3 响应与流式 → §4 错误码 → 其余细节。每个结论后面附 `path:line` 出处，便于回查（路径相对 `_ref/panel` 与 `_ref/hub`，那两个参考仓库不包含在本仓库内）。

模块对应关系：

| 本文小节 | 实现位置 |
| --- | --- |
| §0 传输、§1 请求头 | `wb2api/identity.py`（`chat_headers` / `ChatMeta` / 会话 id 派生）、`wb2api/upstream.py`（连接与超时） |
| §2 请求体改写 | `wb2api/adapters/chat.py`（`prepare_request` 的固定管线） |
| §3 响应与流式 | `wb2api/adapters/chat.py`（`stream_frames` / `aggregate` / `normalize_usage`）、`wb2api/sse.py`、`wb2api/markup.py` |
| §4 错误码 | `wb2api/service.py`（`_absorb` 决策表与 `_client_error` 信封映射）、`wb2api/pool.py`（五类时间窗） |
| 指纹净化 | `wb2api/sanitize.py`（默认关闭，`prompt.mode="sanitize"` 时启用） |
| 模型目录 | `wb2api/models.py` |

---

## 附：原始调研记录（英文，带源码行号）

Sources: `panel/internal/upstream/*.go` (Go reference impl) and `hub/wb_proxy.py` + `hub/wb_identity.py` + `hub/wb_catalog.py` (Python reference impl). Citations are `path:line` relative to `_ref/panel` and `_ref/hub`.

## 0. Transport

- Single chat endpoint: `POST {chat_base}/v2/chat/completions` (`panel/internal/upstream/client.go:853` `chatCompletionsPath = "/v2/chat/completions"`; `hub/wb_proxy.py:978` `CHAT_PATH = "/v2/chat/completions"`).
- The `/console/chat/completions` twin was removed; both realms now use `/v2` (`client.go:855-862`: "`/console` path on global is WAF-403 for shell-ish body content, `/v2` same base equivalent").
- Bodies are compact JSON, no spaces: `json.dumps(body, ensure_ascii=False, separators=(",",":"))` (`wb_proxy.py:14637-14646`) — comment says default separators are a machine fingerprint in body bytes/Content-Length.
- `chat_base` differs per realm: cn `https://copilot.tencent.com`, global `https://www.workbuddy.ai`; `X-Domain` mirrors it (cn: account domain or `X-No-Department-Info: 1`; global: literal `www.workbuddy.ai`, `client.go:249-255`).
- Upstream **only** streams. Non-stream is a local aggregation of the SSE body (§3).

Required request headers (all of them; any omission is a 400):

```
Content-Type: application/json
Accept: application/json, text/event-stream          # chat; plain "application/json" elsewhere
X-Requested-With: XMLHttpRequest
X-CodeBuddy-Request: 1                               # mandatory risk-control gate ("D1")
Origin: https://www.codebuddy.cn | https://www.workbuddy.ai
Referer: <origin>/ 
Accept-Language: zh-CN | en-US
User-Agent: WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1     # cn
            WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1  # global
Authorization: Bearer <token>
X-User-Id: <uid>
X-Enterprise-Id | X-No-Enterprise-Id: 1
X-Domain: <domain> | X-No-Department-Info: 1
X-Conversation-ID: <32 hex>
X-Conversation-Request-ID: <32 hex>
X-Conversation-Message-ID: <32 hex>
X-Request-ID: <same as message id>
X-Root-Request-ID: <conv req id>
X-Trace-ID / X-B3-TraceId / X-B3-SpanId / X-B3-Sampled: 1
X-Agent-Purpose: conversation ; X-IDE-Name/Type: WorkBuddy ; X-IDE-Version: 5.5.4 ; X-Product: WorkBuddy
X-Machine-ID / X-Session-ID: sha256("wb2a:"+purpose+":"+uid) hex, first 18 bytes
```

`panel/internal/upstream/headers.go:147-166,198-241,336-347`; `hub/wb_identity.py:504-525`. A missing conversation id is a hard 400 `11-128` "conversation id missing" (`headers.go:269-302`, issue #190). Sending the cn platform UA segment to the global realm can trigger 403 `11140` (`headers.go:59-60`). Static API keys `ck_*`/`pt_*` must additionally send `X-API-Key` — with only `Authorization`, `/v3/config` returns 54 models instead of 56 (`wb_identity.py:625-626`). CLI/VSCode identities gzip the body and set `Content-Encoding: gzip` (`wb_proxy.py:16535-16536`); the desktop identity does not.

## 1. REQUEST body

Forwarded verbatim (unknown top-level keys are preserved: Go unmarshals into `map[string]any` and re-marshals, `payload.go:30-36`; Python copies the dict, `wb_proxy.py:14572`):

`model`, `messages`, `tools`, `temperature`, `top_p`, `n`, `presence_penalty`, `frequency_penalty`, `logprobs`, `top_logprobs`, `response_format`, `user`, `parallel_tool_calls`, `seed`, plus WorkBuddy-specific `thinking`, `reasoning_effort`/`reasoningEffort`, `prompt_cache_key`, `cache_retention`.

Normalized / injected on the way out:

| field | rule | cite |
|---|---|---|
| `stream` | forced `true` unconditionally (upstream rejects non-stream) | `payload.go:38`, `wb_proxy.py:14631` |
| `stream_options` | injected only when absent: `{"include_usage": true}` | `payload.go:48-50`, `wb_proxy.py:14632-14633` |
| `max_completion_tokens` | always deleted; copied to `max_tokens` only if `max_tokens` absent and value is int > 0 | `payload.go:101-126`, `wb_proxy.py:14229-14240` |
| `max_tokens` | if still absent → catalog `maxOutputTokens` (hub); for `gpt-*` models raised to ≥ 16 (`const gptMinMaxTokens = 16`), else 400 `11133` | `wb_proxy.py:14600-14603`, `payload.go:129-158` |
| `tool_choice` | must be a **string**; `{"type":"none"}`→`"none"`, `{"type":"auto"/"required"}`→ bare string, `{"type":"function","function":{"name":"x"}}`→ `"x"`, anything else deleted (object form = 400 `11101`) | `payload.go:382-425`, `wb_proxy.py:13546-13586` |
| `tools` | `functions[]` legacy promoted; `type` forced to `"function"`; names must match `^[A-Za-z0-9_-]{1,64}$` else the tool is dropped; duplicates dropped; params schema repaired: `type:"object"`, `properties:{}`, `required` cleaned, `str/int/dict/list/float/bool/text` type aliases mapped, null `description/title/format/pattern/items/default` stripped (else 400 `11129`) | `wb_proxy.py:13719-13790,13602-13710` |
| `stop` | upstream type is `[]string`; string → `[s]`; array filtered to non-empty strings; `""`/`[]`/`null`/other → field deleted (string stop = 400 `11101`, measured twice: `"stop":"END"`→400, `"stop":["END"]`→200) | `wb_proxy.py:13794-13817` |
| `messages[].role` | `developer` → `system` (other roles rejected with 400 `11-128`) | `payload.go:237-256`, `wb_proxy.py:12939-12941` |
| `messages[].content` parts | `image_url` given as a string → `{"url": "<str>"}`; parts of `type: "reasoning_text"`/`"reasoning"` dropped; message dropped if it becomes empty and has no `tool_calls` | `payload.go:267-344` |
| `prompt_cache_key` | optional; when enabled `"="wb2a-"+uid[:8]+"-"+sha256(uid+"\|"+conv)[:16]`; the upstream already caches ~8k prefixes without it (`prompt_cache_hit_tokens=9600`), so it is opt-in | `cache_key.go:35-79`, `wb_proxy.py:14861-14904` |
| `system` prompt | **hub**: prepends `{"role":"system","content":"You are a helpful assistant."}` whenever `messages[0].role != "system"` (both realms); **Go**: only for global, same string | `wb_proxy.py:14565-14566,978`, `payload.go:350-374` |
| keys starting with `_` | dropped (Responses-path markers) | `wb_proxy.py:14572-14573` |

There is **no** `session_id`, `conversation_id`, `client_name`, `search`, `mode` or `language` field in the body — conversation identity is carried by `X-Conversation-*` headers only, and the upstream does not use them to recall history (`wb_identity.py:470-472`: replaying the same id with only system+user still fails to recall the previous turn).

Minimal bodies as they appear verbatim in tests:

```json
{"model":"glm-5.2","messages":[]}                                    // client_test.go:301
{"model":"glm-5.2","messages":[],"reasoning_effort":"max"}           // client_test.go:352
{"model":"deepseek-v4-flash","messages":[]}                          // useragent_test.go:95
{"model":"glm-5.2","messages":[{"role":"system","content":"你是助手"},{"role":"user","content":"你好"}],"reasoning_effort":"high"}  // payload_test.go:198
{"model":"deepseek-v4","messages":[{"role":"user","content":"写个函数"}],"tool_choice":{"type":"auto"},"tools":[{"type":"function","function":{"name":"f"}}]}  // payload_test.go:199
```

Effective wire body for a deepseek turn (field order from `build_upstream_body`, `wb_proxy.py:14538-14633`):

```json
{"model":"deepseek-v4.1-flash","messages":[{"role":"system","content":"You are a helpful assistant."},{"role":"user","content":"你好"}],"thinking":{"type":"enabled"},"reasoning_effort":"high","max_tokens":128000,"stream":true,"stream_options":{"include_usage":true}}
```

## 2. Model ids & catalog

Two upstream catalog endpoints, both GET with the standard headers plus `Authorization: Bearer`:

1. `GET {base}/v3/config` — **preferred/authoritative**; envelope `{code, data:{models, productFeaturesConfig:{ModelTrialBanner:{banners:[{modelId,targetModelId}]}}, modelPromotions, modelTiers[]}}` (`client.go:1857-1958`, `wb_proxy.py:12373`). Extra headers: `Accept: application/json, text/plain, */*`, `X-Product: SaaS`, `X-User-Id`, `X-Domain`, `User-Agent: CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0`. `code != 0` → error `v3/config code=%d`.
2. `GET {base}/console/enterprises/personal/models` — narrower list; envelope `{code, data:{models:[…], agents:[{name,models:[…]}]}}`; the allowlist is `agents[name=="cli"].models`, and entries not in it are dropped (`client.go:1535-1579`). Hub's `MODELS_PATH = "/v2/enterprises/personal/models"` returns an even narrower list, which is why `/v3/config` is preferred (`wb_proxy.py:977,1054-1056,12813`).

The endpoint is **UA-sensitive**: `CLI/2.63.2 CodeBuddy/2.63.2` yields 22 chat models (including `deepseek-v4.1-flash`, `deepseek-v4.1-flash-sg`, `gpt-6-astra`, `kimi-k2.8-preview`), `CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0` yields 14 entries with richer per-model fields (26 003 B vs 21 111 B payload); a wrong/old UA is 400 `code=12403` (`client.go:1410-1422,1854`). Cache: 1 h on success, 5 min negative (`global_models.go:65-68`).

One catalog entry (raw desktop payload; `hub/wb_catalog.py:20-137`):

```json
{
  "id": "deepseek-v4.1-flash",
  "name": "Deepseek-V4.1-Flash",
  "descriptionEn": "DeepSeek flagship model, supporting 1M context window, native multimodal model",
  "descriptionZh": "DeepSeek 旗舰模型，支持 1M 上下文窗口，原生多模态",
  "credits": "x0.11",
  "maxInputTokens": 1000000,
  "maxOutputTokens": 128000,
  "maxAllowedSize": 1000000,
  "contextWindow": {"defaultLength": 300000, "supportedLengths": [300000, 1000000]},
  "supportsImages": true,
  "supportsToolCall": true,
  "supportsReasoning": true,
  "onlyReasoning": true,
  "reasoning": {"defaultEffort": "high", "summary": "auto", "supportedEfforts": ["low", "high", "max"]},
  "relatedModels": {"lite": "deepseek-v4.1-flash", "reasoning": "deepseek-v4.1-flash"},
  "temperature": 1,
  "vendor": "f"
}
```

Field notes: `credits` is a display/routing multiplier string, either `"x0.11"` or `"x0.34 credits"` (`wb_catalog.py:45,107`); `"x0.00"` is the free tier (`wb_proxy.py:4045`). `maxInputTokens`/`maxOutputTokens` are the context window and the shared reasoning+answer budget (there is no separate thinking cap, `client.go:1279-1310`). `reasoning.summary: "auto"` and `reasoning.effort` (fixed effort, old key) both appear; `defaultEffort`/`supportedEfforts`/`canDisableThinking` are the newer keys (`client.go:1316-1343`). `relatedModels.lite`/`.reasoning` is the aliasing mechanism; when a free `x0.00` sibling exists it is the advertised one (`wb_proxy.py:12512-12527`). Trial models present in `ModelTrialBanner.banners` but absent from `data.models` inherit capability fields from `targetModelId` but get `credits: ""` and no tags (`client.go:1930-1948`).

Exclusion filter for chat use (`nonChatModel`, `client.go:1386-1403`): id prefix `nes-`/`completion-`/`codewise-`, or `maxOutputTokens <= 256`, or tags containing `text-to-image|image-to-image|text-to-video|image-to-video`.

Unknown-model handling: no error is raised at catalog time. Lookup falls back catalog → static table → `{}` (`wb_proxy.py:14649-14673` cache → `STATIC_CN_MODELS`/`STATIC_INTL_MODELS` → `wb_catalog.STATIC_MODELS`; `model_catalog.go:234-290` upstream `ModelInfo` → static → `model.json` → models.dev → 1 M default), and the id is forwarded verbatim. The upstream then returns 400 `11102` ("model not available", `client.go:281-302`) or `11158` ("is not allowed for user type"); `11158` is now rejected earlier from the `/v3/config` `modelTiers[]` map (`wb_tiers.py:1-6`, `wb_proxy.py:16773-16779`). Realm is inferred from the model id (`detect_model_realm`, `wb_proxy.py:16290`).

## 3. RESPONSE (non-stream)

Non-stream is synthesized locally from the SSE body (`sse.go:30-266` `Aggregate`; `wb_proxy.py:17501-17650` `aggregate_stream`). Exact output:

```json
{
  "id": "chatcmpl-1",
  "object": "chat.completion",
  "created": 1753600000,
  "model": "glm-5.2",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "你好，世界",
      "reasoning_content": "……",
      "tool_calls": [{"id":"call_a","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"北京\"}"}}]
    },
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens":5,"completion_tokens":2,"total_tokens":7,
            "prompt_tokens_details":{"cached_tokens":0},
            "cache_read_input_tokens":0,"prompt_cache_hit_tokens":0}
}
```

- Thinking text lives in `message.reasoning_content` (string), never in `content` (`sse.go:180-201`, `wb_proxy.py:17591-17595`).
- `reasoning_content` is omitted when empty; `tool_calls` carries no `index` in the non-stream form (`dsml.go:453-466`).
- `usage` is normalized first: `total_tokens` synthesized as `prompt_tokens + completion_tokens` when missing (`sse.go:273-288`); cache aliases rewritten to the best available hit value (`usage.go:10-34`).
- `finish_reason`: `stop` when the model answered; `tool_calls` when any real tool call exists (including one recovered from inline markup, `wb_proxy.py:17612-17624`); `length` when the stream was truncated.
- Hub adds two non-OpenAI keys: `elapsed_ms` and `first_chunk_at` (`wb_proxy.py:17638-17650`).
- If usage is missing entirely, hub estimates it: `prompt_tokens = max(1, completion//2)`, `completion_tokens = completion`, `total = sum`, `completion_tokens_details.reasoning_tokens = estimate_tokens(reasoning)`, `prompt_tokens_details.cached_tokens = 0` (`wb_proxy.py:17625-17637`).

## 4. RESPONSE (stream) — SSE grammar

Each event is one line `data: <json>\n\n` terminated by blank line. `data: [DONE]` ends the stream and is the **only** normal terminator (`sse.go:148-153`; hub tolerates `data:` with no space, leading `:comment` and repeated `data:` prefixes — `wb_proxy.py:12828-12838`). Go's reader requires the literal prefix `"data: "` and ignores everything else silently (`sse.go:148`), passing non-`data:` comment lines through unchanged (`sse.go:727-735`).

Content delta:

```
data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1753600000,"model":"glm-5.2","choices":[{"index":0,"delta":{"role":"assistant","content":"你好"}}]}
data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1753600000,"model":"glm-5.2","choices":[{"index":0,"delta":{"content":"，世界"}}]}
data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1753600000,"model":"glm-5.2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}
data: [DONE]
```

(`sse_test.go:74-77`.) Reasoning delta arrives in the same shape with `"delta":{"reasoning_content":"…"}`. Usage normally rides the finish chunk, and may be `null` on other frames (`sse_test.go:121-131`).

Tool-call assembly (`sse_test.go:121-131`, verbatim):

```
data: {"id":"x1","model":"deepseek-v4-pro","created":1,"choices":[{"index":0,"delta":{"role":"assistant","content":"","tool_calls":[{"id":"call_a","type":"function","function":{"name":"get_weather","arguments":""},"index":0}]}}],"usage":null}
data: {"id":"x1","choices":[{"index":0,"delta":{"tool_calls":[{"function":{"arguments":"{\"city\":"},"index":0}]}}]}
data: {"id":"x1","choices":[{"index":0,"delta":{"tool_calls":[{"function":{"arguments":"\"北京\"}"},"index":0}]}}]}
data: {"id":"x1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"total_tokens":11}}
data: [DONE]
```

Accumulation rule: key by `index`; `id`/`type`/`function.name` overwrite when non-empty; `function.arguments` is **string-concatenated** (`sse.go:309-335`, `wb_proxy.py:17563-17585`). A frame may omit `id`/`model`/`created` entirely.

Error frame (mid-stream, always passed through byte-identical, never normalized, and terminates the turn — never followed by `[DONE]` semantics):

```
data: {"error":{"code":6004,"message":"模型限流，将在 2026-09-27 01:00:00 重置"}}
```

(`sse_test.go:841`, `panel/internal/upstream/sse.go:692-701`.) Optional non-replacing `error.gateway_hint` is attached by the gateway (`wb_proxy.py:14961-15023`).

Frame whitelist a reimplementation must emit (`normalizeFrame`, `sse.go:391-467`): top level keeps only `id`, `object`, `created`, `model`, `system_fingerprint`, `service_tier`, `usage`; per choice keeps `index`, `delta{role,content,reasoning_content,refusal,tool_calls,function_call}`, `finish_reason` (set to `null` when empty). `object` defaults to `"chat.completion.chunk"`, `id` defaults to `"chatcmpl-wb2api"`; `delta` keys that are empty strings/arrays are dropped and a frame reduced to `{"delta":{}}` with no `finish_reason` is suppressed (`wb_proxy.py:12839-12880`).

Upstream dialect caveat: the framing is OpenAI-compatible, but **tool calls may also arrive as inline text markup inside `content`** (DSML, §5) and `delta.function_call` may appear as a placeholder `{"name":"","arguments":""}` — it must be discarded, never converted into a call (`wb_proxy.py:17563-17585`).

## 5. TOOL CALLS

Native path: standard `delta.tool_calls[]` with `index`, `id`, `type:"function"`, `function.name`, `function.arguments` chunks (§4). Re-emit as one full `tool_calls` entry per call, either inline in the delta or as one chunk per call:

```json
{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"北京\"}"}}]}
```
(`wb_proxy.py:14171-14181`.) Keep `function.name` only on the first chunk per index (`stripToolCallNames`, `sse.go:352-385`).

Inline markup path (real upstream leak, `panel/internal/upstream/testdata/leaked_markup.txt`):

```
我先看一下仓库结构和图片内容，再定位价格同步逻辑。

<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="exec_command">
<｜｜DSML｜｜ parameter name="cmd" string="true">cd /Users/... && git status --short</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>
```

Grammar: `<` + `U+FF5C U+FF5C` + `DSML` + `U+FF5C U+FF5C` + tag; open tags `<… calls>`, `<… invoke name="X">`, `<… parameter name="Y" string="true|false">`; close tags `</… calls>`, `</… invoke>`, `</… parameter>`; `string="true"` means the body is a raw string, otherwise it is parsed as JSON (`dsml.go:51-83,294-333`).

Go matcher regexes (`dsml.go:71-72`, verbatim, with `markupTagPrefix = regexp.QuoteMeta("<｜｜DSML｜｜")`):

```
markupInvokeRe = markupTagPrefix + `\s+invoke\s+name="([^"]*)"\s*>`
markupParamRe  = markupTagPrefix + `\s+parameter\s+name="([^"]*)"(?:\s+string="(true|false)")?\s*>`
```

Hub permissive regexes (`wb_proxy.py:13821-13824`, verbatim):

```python
TAG_START       = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE   = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>", re.DOTALL)
DSML_INVOKE_RE  = re.compile(TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>", re.DOTALL)
DSML_PARAM_RE   = re.compile(TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>", re.DOTALL)
```

plus a hybrid parser for half-standard output (`wb_proxy.py:13871-14008`): params may use `<parameter|param|arg|argument>`, the DSML prefix may be omitted on open tags, and bars may be ASCII `|` or fullwidth `｜` (`\uff5c+`); wrapper tags `calls|tool_calls|function_calls|function_call`; attributor = nearest preceding non-wrapper/non-param tag, name from its `name=` attr else its tag name.

Conversion rules:
- One `invoke` = one `tool_call`; params are collected into a JSON object; hub always stringifies all values, Go JSON-parses unless `string="true"` (`dsml.go:294-333`, `wb_proxy.py:13825-13847`). `arguments` is `json.dumps(params, ensure_ascii=False)`; no params → `"{}"`.
- ids: `"call_%d_%d" % (time.Now().UnixNano(), idx)` (`dsml.go:493-495`), `_new_id("call_")` in hub.
- In stream mode the markup must be filtered incrementally with a bounded hold buffer (`markupMaxBlockBytes = 256*1024` in Go; `MAX_HOLD = 262144` in hub), releasing text as soon as the tail can no longer become markup, and flushing at stream end (`dsml.go:149-218`, `wb_proxy.py:14040-14151`).
- Non-stream: only when `choices[0]` has no `tool_calls` and `finish_reason == "stop"`; then `content = <text before/after block>` and `finish_reason = "tool_calls"` (`dsml.go:410-448`, `wb_proxy.py:14011-14037`).
- Tool-name acceptance: strict mode requires the name to be in `tools[].function.name`; weak mode requires `^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$` (`dsml.go:78,247-287`). Unparsable blocks are re-emitted as text verbatim.

Argument robustness: a call whose `arguments` is non-empty and not valid JSON is truncated and dropped when `finish_reason == "length"` or `[DONE]` was never seen (`truncation.go:21-47`, `wb_proxy.py:17607-17611`). Calls with an empty `function.name` are dropped (`wb_proxy.py:17598-17602`).

History repair, applied in this fixed order on every outbound request (`payload.go:65-72`, `wb_proxy.py:14582-14585`):

1. `mergeAdjacentToolCalls` — merge consecutive assistant messages (second has `tool_calls` and empty content, or is a text-only assistant following a tool_calls-only assistant); `reasoning_content` joined with `"\n"` (`tool_pairing.go:273-344`).
2. `repackToolResultBlocks` — move interleaved non-tool messages behind the whole batch of `role:"tool"` results belonging to the preceding assistant `tool_calls` (`tool_pairing.go:35-110`).
3. `cleanupOrphanToolCalls` — drop `tool_calls` entries with no matching `tool_call_id` and `role:"tool"` messages with no matching call; delete the key when the list empties (`tool_pairing.go:123-240`).

Not repairing this yields `400` / `code 11148` `tool_call_sequence_broken`: "tool calls and tool results do not match, please start a new conversation and retry" (reproduced 2026-09-20 on `deepseek-v4.1-flash`).

## 6. THINKING / REASONING

Request side (deepseek family only; `isDeepSeekModel = strings.HasPrefix(lower(trim(model)), "deepseek")`, `thinking.go:47-49`):

- `thinking` object: `{"type":"enabled"}` or `{"type":"disabled"}`; absent + deepseek ⇒ the gateway sets `{"type":"enabled"}` (`thinking.go:156-186`).
- `reasoning_effort` must be present **as well** — `thinking.type=enabled` alone yields `reasoning_tokens: 0` and an empty `reasoning_content`. Measured on `deepseek-v4.1-flash`, same prompt: `enabled + no effort → reasoning_tokens 0 / reasoning_content len 0`; `reasoning_effort=high → reasoning_tokens 37 / reasoning_content len 117` (`wb_proxy.py:14608-14611`).
- Defaults: Go `defaultDeepSeekEffort = "high"` (`thinking.go:32`); hub picks catalog `reasoning.defaultEffort`, else `"high"` when the mode is `high`, and honors env `WB_DEFAULT_REASONING_EFFORT=off|none` to disable injection (`wb_proxy.py:14789-14804,14624-14628`). Forced `high` makes deepseek spend the whole round reasoning and sometimes return no answer text (issue #508).
- Effort must be one of the model's `reasoning.supportedEfforts` (`off|minimal|low|medium|high|xhigh|max`, rank map `payload.go:161`), downgraded to the highest supported ≤ requested (`payload.go:168-224`, `wb_proxy.py:14727-14786`). `glm-5.1` is special: missing/`none`/`disabled` effort returns `400 code 11150` "the reasoning effort value is not supported by the current model", so an explicit `medium`-class effort is forced (`wb_proxy.py:14824-14858`).
- Assistant history must echo reasoning: every assistant message needs `reasoning_content` (string) **and** `reasoning` (string). Upstream rejects absent/`null`/empty with `400`; whitespace passes — so emit `" "` when there is nothing to echo (`thinking.go:70-144`, `wb_proxy.py:13504-13542`).

Response side: reasoning arrives as `delta.reasoning_content` (stream) / `message.reasoning_content` (aggregated) — same key in both modes; there is no `reasoning` key in responses and no Anthropic-style `thinking` block. Gateways must forward it untouched and must **not** fold it into `content`.

Client-prompt sanitization is outbound-only and is never undone on the response path: the gateway rewrites exactly-matched fingerprint sentences before sending (identity sentences such as `You are Claude Code, Anthropic's official CLI for Claude` → `You are a helpful AI assistant.`, `x-anthropic-billing-hdr`, `cc_*=…` key/value pairs, and the literal string `11-128`) and strips nothing from replies (`panel/internal/upstream/sanitize.go:50-219`, `wb_proxy.py:12947-13061`). Rationale: the upstream moderation is a verbatim/substring check — changing one word in the sentence clears the 400, and any request body containing the bare literal `11-128` is rejected regardless of context.

## 7. USAGE & CREDITS

Upstream `usage` object keys observed (`usage.go:10-34`, `sse_test.go:818-827`):

```json
{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7,
 "prompt_tokens_details":{"cached_tokens":7808},
 "cache_read_input_tokens":0,"prompt_cache_hit_tokens":0,
 "prompt_cache_miss_tokens":0,
 "completion_tokens_details":{"reasoning_tokens":37}}
```

Cache reporting is deliberately redundant and self-contradictory: the real hit lives in `prompt_tokens_details.cached_tokens`, while `cache_read_input_tokens` / `prompt_cache_hit_tokens` / `cached_tokens` are often emitted as `0`. Normalization must pick the first non-zero of `prompt_tokens_details.cached_tokens | prompt_cache_hit_tokens | cache_read_input_tokens | cached_tokens | input_tokens_details.cached_tokens` and rewrite all of those aliases to that value (`usage.go:10-34`). Miss = `prompt_cache_miss_tokens` when present, else `prompt_tokens - hit` (negative ⇒ unavailable) (`usage.go:104-137`).

Credits: the upstream charges a per-model multiplier and reports it only in the catalog as a string (`"x0.11"`, `"x0.34 credits"`, `"0.50x"`); it never appears in the response body. There is **no** local pricing math in either project: `credits` is used for display and for routing weight, and `models.dev` is consulted only for `limit.context`/`limit.output` caps (`modelsdev.go:14,261-275`), never for price. Normalization: strip a trailing `credits`, strip a leading or trailing `x`, `ParseFloat` (`normalizeModelRate`, `client.go:1768-1789`); `"x0.00"`/`"0"`/`"0.00"` = free tier (`wb_proxy.py:4045`). Promotions replace the multiplier: `discount.factor` with `badge.label` and `schedule.daily[]` windows in `CST` (`client.go:1609-1740`). Actual debit is measured upstream; the gateway only reconciles by sweeping the account balance and by counting the 6004 quota marker (`wb_proxy.py:16644-16666`, `wb_accounts.py:826-828`).

## 8. ERROR CONTRACT

Envelope (both projects build the same shape for clients):

```json
{"error":{"message":"...","type":"...","code":... ,"gateway_hint":"..."}}
```

with `code` a string/number mirror of the upstream body, optional `gateway_hint` (never replaces the message, `wb_proxy.py:14961-15023`), and for 429 additionally `"retry_after": <sec>`, `"reset_at": <epoch>` plus `Retry-After` and rate-limit headers (`wb_proxy.py:20555-20597`).

| upstream | trigger / literal | client-facing | action |
|---|---|---|---|
| 400 `11-128` | "Illegal API invocation from an unapproved channel" — body/system text contains a fingerprint (Claude Code / Codex CLI identity sentence, `x-anthropic-billing-hdr`, the bare literal `11-128`), or a missing `X-Conversation-ID` | 400 `invalid_request_error` | sanitize the prompt (one-word rewrite suffices), retry once; never rotate accounts |
| 400 `11101` | `Unmarshal chat params failed with error: json: cannot unmarshal string into Go struct field Request.stop of type []string` — object `tool_choice`, string `stop`, dict `arguments` | 400 | normalize the field (`stop`→array, `tool_choice`→string), retry |
| 400 `11129` | `invalid function call parameters` — schema without `type:"object"`/`properties`, escaped `\_` in `pattern` | 400 | repair schema, retry |
| 400 `11133` | `model_param_invalid`; path `tools[N].function.name` empty or `max_tokens < 16` on gpt models | 400 | fix tool name / clamp to 16 |
| 400 `11135` | `invalid_image_data` / "replace the image" | 400 | resend a valid image part |
| 400 `11102` | model not available (`ModelBlockReason`) | 400/404 | pick another model, no account penalty |
| 400 `11158` | "is not allowed for user type" — tier/plan gate from `/v3/config` `modelTiers[]` | 400 | pick another model |
| 400 `11115` / 413 | `prompt is too long`, `context length` | 400/413 | shrink context; never retry unchanged |
| 400 `11148` | `tool_call_sequence_broken`: "tool calls and tool results do not match…" | 400 | repair history (merge/repack/cleanup), retry |
| 400 `11150` | "the reasoning effort value is not supported by the current model" (glm-5.1 without effort) | 400 | force a supported effort |
| 400 `12403` | wrong/expired `User-Agent` on `/v3/config` | 400 | fix UA |
| 401 `12153` | "Offline user session not found" | 401 | refresh token once, then park the account 60 s |
| 402 | insufficient credits | 402 | park until 04:00, rotate account |
| 403 `11140` | "request illegal" / 账号存在异常行为 — risk control on the device/account | 403 | stop: 1–3 working-day appeal; **do not** rotate accounts or change headers (`RESTRICTED_403_ROTATE_MAX = 0`) |
| 403 non-11140 | content review / WAF (APISIX page, not the JSON envelope) | 403 `permission_error` | degrade or rewrite the prompt (retry once), else surface as `ContentRejected` |
| 429 `6004` | `{"code":6004,"message":"模型限流，将在 2026-09-27 01:00:00 重置"}` — the **only** marker of "quota exhausted" | 429 + `Retry-After` | cooldown until `reset_at` (parse `将在 (.+?) 重置` / `reset at YYYY-MM-DD HH:MM:SS`, UTC+8), rotate model/account |
| 429 other | `rate limit`, `too many requests`, `请求过于频繁` | 429 | soft 60 s → 10 min ladder |
| 503 / `6021` | `queue.full.title` — concurrency slot busy | 429 wait 1 s (a 503 makes clients retry every 24 ms, issue #665) | backoff |
| 5xx + `11102/11129/11134/11150/11158` | model-scoped failure | pass through 5xx | do not penalize the account |

Classification helpers to copy: `Classify(status, body)` with `hardMarkers` (insufficient credit, quota exceeded, 积分不足, 额度不足, 余额不足 …), `softRateMarkers` (rate limit, too many requests, 限流), `sessionDeadMarkers` ("Offline user session not found", "12153"), `accountFaultMarkers` ("trial not activated"), `contentBlockedMarkers` ("blocked by security policy", "unapproved channel", "illegal api invocation") (`client.go:104-236`). Two distinct errors share the message `request illegal` and are separated **only** by code: `11140` = account restricted, `11-128` = channel refused (`client.go:161-182`).

## 9. STREAMING ROBUSTNESS

- Heartbeat: gateway→client comment frame `: keepalive\n\n` every `SSE_HEARTBEAT_SECONDS = 15.0`, written by a daemon thread holding the same writer lock, tick `min(0.1, interval/4)` (`wb_proxy.py:17440-17497`).
- Timeouts: header (connect/TLS/first byte) 120 s, per-read idle 300 s, both overridable and clamped `max(1.0, x)`, no total-duration cap (`wb_proxy.py:16179-16202`). Go: `Client.IdleTimeout` cancels the request when no bytes arrive; ticker period `idle/4` clamped to `[10ms, 1s]`, first byte governed by `Transport.ResponseHeaderTimeout` (`idle.go:49-87`).
- Truncation detection: a clean EOF is only `b""` when `Content-Length` says 0; otherwise raise `http.client.IncompleteRead` (`wb_proxy.py:17420-17438`). Go additionally treats a stream without `[DONE]` as truncation and drops unparsable tool arguments in that case (`sse.go:210-242`).
- Empty stream: emit exactly one raw frame `{"error":{"message":"empty upstream stream","type":"server_error"}}` then `data: [DONE]\n\n` and return `errEmptyStream` (`sse.go:771-783`, `wb_proxy.py:26022-26035`).
- Mid-stream failure after bytes were sent: never a bare `[DONE]`; send `data: {"error":{"message":"upstream stream aborted: …","type":"upstream_error","param":null,"code":"upstream_stream_error"}}\n\n` and close (`wb_proxy.py:14208-14218,26045-26095`).
- Reconnect: at most **one** transparent reopen, and only if nothing has been emitted yet (`reopen` + `headers_sent=True, retried=True`, `wb_proxy.py:26045-26095`). Go counts transport failures per attempt and closes idle connections before retrying (`client.go:1234-1242`).
- Response headers: `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache`, `Connection: close` (hub) / `Connection: keep-alive` + `X-Accel-Buffering: no` (Go `sse.go:514-556`).
- Frame hygiene: rewrite every chunk `id` to one per-stream id so reasoning and answer frames pair up; hoist `usage` into its own choices-less frame **after** the finish chunk and **before** `[DONE]` (strict parsers and billing gateways ignore usage on choice-bearing chunks, `wb_proxy.py:14188-14205,25976-26028`); hold `finish_reason` until the filtered content tail is flushed so a synthesized `tool_calls` finish wins (`wb_proxy.py:26000-26035`).
- Client aborts (BrokenPipe/ConnectionReset) are recorded as `outcome="client_aborted"` and must not be counted as upstream failures (`wb_proxy.py:26036-26044`).

## MUST-REPLICATE CHECKLIST

- `POST /v2/chat/completions`, compact JSON, `stream:true` forced, `stream_options:{"include_usage":true}`.
- Full header set including `X-CodeBuddy-Request: 1` and the five `X-Conversation-*`/`X-Request-ID` ids.
- `stop` always `[]string`; `tool_choice` always a string; `max_completion_tokens` deleted; gpt `max_tokens ≥ 16`.
- Tool schema repair (`type:"object"`, `properties:{}`, `required` cleaned, type aliases) and tool-name regex `^[A-Za-z0-9_-]{1,64}$`.
- `developer` → `system`; `image_url` string → `{"url":…}`; drop `reasoning_text` parts.
- History trio in order: merge adjacent tool_calls assists → repack tool results → cleanup orphans.
- deepseek: `thinking:{type:"enabled"}` **and** `reasoning_effort` (default `high`, downgraded to `supportedEfforts`); echo `reasoning_content` + `reasoning` (space fallback) on every assistant message.
- System prompt `"You are a helpful assistant."` when none provided.
- Accept both native `delta.tool_calls` (index-keyed, arguments concatenated) and inline DSML markup; incremental filter with 256 KiB hold cap; `finish_reason` `stop`→`tool_calls`.
- Non-stream: aggregate, synthesize `total_tokens`, drop truncated args on `length`/no-`[DONE]`.
- Usage: reconcile cache aliases to `prompt_tokens_details.cached_tokens`; header/idle timeouts 120 s/300 s; `: keepalive` every 15 s.
- Errors: pass `{"error":{code,message}}` frames through untouched; 429 with `Retry-After`/`reset_at`; 403 `11140` = stop, do not rotate; 429 `6004` = quota exhausted.
- Outbound fingerprint sanitization (`11-128`, `x-anthropic-billing-hdr`, `cc_*=`, identity sentences) — outbound only, never applied to responses.
