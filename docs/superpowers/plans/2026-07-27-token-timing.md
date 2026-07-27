# Token Timing JSONL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `--token-timing-file PATH` mode to `examples/main/main.cpp` that records per-generated-token timing and a final JSONL summary without changing default behavior.

**Architecture:** Reuse `gpt_params` and the existing example-scoped argument registry for the new option. Keep the timing state and JSONL writing local to `main.cpp`, opening the file only when enabled; start timing immediately before initial prompt processing and record each sampled token after its text piece is produced.

**Tech Stack:** C++11, existing `gpt_params` argument parser, `ggml_time_us()`, `std::ofstream`, repository `nlohmann::json` (`common/json.hpp`), CMake/CTest.

## Global Constraints

- Only `LLAMA_EXAMPLE_MAIN` accepts `--token-timing-file PATH`.
- Unset `--token-timing-file` means no extra timing calls, no timing file, and no stdout changes.
- Model loading time is excluded; timing starts before initial prompt processing.
- Token events use `id`, `piece`, `elapsed_ms`, and `itl_ms`; the first token’s `itl_ms` equals its TTFT.
- Summary uses `prompt_tokens`, `generated_tokens`, `ttft_ms`, `total_generation_ms`, and `average_itl_ms`; average ITL includes the first token’s TTFT.
- All timing uses `ggml_time_us()` and JSON output is one compact object per line.

---

### Task 1: Add the failing argument-parser test

**Files:**
- Modify: `tests/test-arg-parser.cpp:65-110`

**Interfaces:**
- Consumes: existing `gpt_params_parse()` and `LLAMA_EXAMPLE_MAIN` enum.
- Produces: a regression assertion that the main example accepts `--token-timing-file PATH`.

- [ ] **Step 1: Write the failing test**

Add a dedicated parser case using a fresh `gpt_params` object and the existing
`list_str_to_char` helper:

```cpp
    gpt_params main_params;
    argv = {"binary_name", "--token-timing-file", "token-timing.jsonl"};
    assert(true == gpt_params_parse(argv.size(), list_str_to_char(argv).data(), main_params, LLAMA_EXAMPLE_MAIN));
```

Place it in the valid-usage section after the existing `--predict`/`--batch-size` case.

- [ ] **Step 2: Run the test to verify it fails for the missing option**

Run:

```bash
cmake -S . -B build -DLLAMA_BUILD_TESTS=ON -DLLAMA_BUILD_EXAMPLES=ON
cmake --build build --target test-arg-parser -j2
ctest --test-dir build -R '^test-arg-parser$' --output-on-failure
```

Expected: the test executable reports the new option as unknown for `LLAMA_EXAMPLE_MAIN` and exits non-zero. This confirms the test exercises the missing feature rather than existing behavior.

- [ ] **Step 3: Commit the red test**

```bash
git add tests/test-arg-parser.cpp
git -c user.name='Codex' -c user.email='codex@openai.com' commit -m "test: cover token timing argument"
```

### Task 2: Register the token-timing argument

**Files:**
- Modify: `common/common.h:225-233`
- Modify: `common/arg.cpp:2067-2078`
- Modify: `tests/test-arg-parser.cpp:valid usage section`

**Interfaces:**
- Consumes: `gpt_params`, `llama_arg::set_examples()`, and the failing parser test.
- Produces: `gpt_params::token_timing_file`, defaulting to an empty string, and a main-only `--token-timing-file PATH` option.

- [ ] **Step 1: Extend the test with default and parsed-value assertions**

Update the test to assert the default and parsed path:

```cpp
    assert(main_params.token_timing_file == "token-timing.jsonl");

    gpt_params default_params;
    assert(default_params.token_timing_file.empty());
```

- [ ] **Step 2: Add the parameter field**

Add this field beside the existing file/path options in `gpt_params`:

```cpp
    std::string token_timing_file    = ""; // JSONL file for per-token timing (empty: disabled) // NOLINT
```

- [ ] **Step 3: Register the main-only command-line option**

Add this option near the existing `--logdir` registration:

```cpp
    add_opt(llama_arg(
        {"--token-timing-file"}, "PATH",
        "write per-token generation timing as JSONL (default: disabled)",
        [](gpt_params & params, const std::string & value) {
            params.token_timing_file = value;
        }
    ).set_examples({LLAMA_EXAMPLE_MAIN}));
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
cmake --build build --target test-arg-parser -j2
ctest --test-dir build -R '^test-arg-parser$' --output-on-failure
```

Expected: `test-arg-parser` passes, including duplicate-argument checks and the new path/default assertions.

- [ ] **Step 5: Commit the parser implementation**

```bash
git add common/common.h common/arg.cpp tests/test-arg-parser.cpp
git -c user.name='Codex' -c user.email='codex@openai.com' commit -m "feat: add token timing file option"
```

### Task 3: Implement opt-in JSONL timing in the generation loop

**Files:**
- Modify: `examples/main/main.cpp:1-45`
- Modify: `examples/main/main.cpp:500-750`
- Modify: `examples/main/main.cpp:940-960`

**Interfaces:**
- Consumes: `params.token_timing_file`, `ggml_time_us()`, `llama_decode()`, sampled token IDs, and token pieces.
- Produces: compact JSONL token events during generation and one summary on normal completion.

- [ ] **Step 1: Add the JSON and timing dependencies**

Include `json.hpp` and `<cstdint>` with the existing local includes. Use `nlohmann::json` only inside the enabled path; do not construct timing objects or call `ggml_time_us()` when `params.token_timing_file` is empty.

- [ ] **Step 2: Add local timing state and file-open handling**

After model/context initialization and before entering generation, create an `std::ofstream` only when the path is non-empty, using truncation mode. If it is not open, log an error with `LOG_ERR` and return non-zero before generation. Store:

```cpp
    const bool token_timing_enabled = !params.token_timing_file.empty();
    std::ofstream token_timing_file;
    bool token_timing_started = false;
    int64_t token_timing_start_us = 0;
    int64_t token_timing_last_us = 0;
    int64_t token_timing_end_us = 0;
    double token_timing_sum_itl_ms = 0.0;
    double token_timing_ttft_ms = 0.0;
    size_t token_timing_index = 0;
```

Use `embd_inp.size()` as `prompt_tokens`, after prompt tokenization/session setup has completed.

- [ ] **Step 3: Start the timer at initial prompt processing**

At the beginning of the first loop iteration that has the initial prompt in `embd`, before session-prefix reuse and the first `llama_decode()` call, initialize `token_timing_start_us = ggml_time_us()` and guard it with a boolean so it runs once. This excludes model loading and includes all initial prompt decode work. If no decode/generation occurs, leave the start unset and emit zero timing values in summary.

- [ ] **Step 4: Record each sampled token after text conversion**

In the sampling branch, after `gpt_sampler_sample()` and after obtaining the same piece used for normal output (`llama_token_to_piece(ctx, id, params.special)`), capture `const int64_t token_now_us = ggml_time_us()` and write:

```cpp
const double elapsed_ms = (token_now_us - token_timing_start_us) / 1000.0;
const double itl_ms = token_timing_index == 0
    ? elapsed_ms
    : (token_now_us - token_timing_last_us) / 1000.0;
nlohmann::json event = {
    {"type", "token"}, {"index", token_timing_index}, {"id", id},
    {"piece", token_str}, {"elapsed_ms", elapsed_ms}, {"itl_ms", itl_ms},
};
token_timing_file << event.dump() << '\n' << std::flush;
```

Update `token_timing_last_us`, `token_timing_sum_itl_ms`, `token_timing_ttft_ms` for the first token, and `token_timing_index` after the write. Keep this entirely conditional so disabled mode adds no conversion or timer work beyond the existing stdout path.

- [ ] **Step 5: Write the final summary**

After the generation loop and before normal cleanup, if timing is enabled, use `ggml_time_us()` for the end timestamp and write:

```cpp
nlohmann::json summary = {
    {"type", "summary"},
    {"prompt_tokens", embd_inp.size()},
    {"generated_tokens", token_timing_index},
    {"ttft_ms", token_timing_ttft_ms},
    {"total_generation_ms", token_timing_start_us == 0
        ? 0.0 : (token_timing_end_us - token_timing_start_us) / 1000.0},
    {"average_itl_ms", token_timing_index == 0
        ? 0.0 : token_timing_sum_itl_ms / token_timing_index},
};
token_timing_file << summary.dump() << '\n' << std::flush;
```

Check the stream state after writes and report an error if output fails. Do not add a summary from the existing SIGINT `_exit` path; normal completion is the defined summary boundary.

- [ ] **Step 6: Build the example and inspect help output**

Run:

```bash
cmake --build build --target llama-cli -j2
build/bin/llama-cli --help 2>&1 | rg -- '--token-timing-file'
```

Expected: the build exits zero and help output contains the new option. Running without the option must not create a timing file or add timing output.

- [ ] **Step 7: Commit the timing implementation**

```bash
git add examples/main/main.cpp
git -c user.name='Codex' -c user.email='codex@openai.com' commit -m "feat: record per-token generation timing"
```

### Task 4: Run full verification and review the diff

**Files:**
- Verify: `common/common.h`
- Verify: `common/arg.cpp`
- Verify: `examples/main/main.cpp`
- Verify: `tests/test-arg-parser.cpp`

**Interfaces:**
- Consumes: all changes from Tasks 1–3.
- Produces: fresh test/build evidence and a clean, scoped final diff.

- [ ] **Step 1: Run the focused parser test and full test build**

Run:

```bash
cmake --build build --target test-arg-parser llama-cli -j2
ctest --test-dir build -R '^test-arg-parser$' --output-on-failure
```

Expected: both targets build successfully and the parser test reports zero failures.

- [ ] **Step 2: Run repository diff checks**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors; only the intended design/plan documentation and source/test files are changed.

- [ ] **Step 3: Review requirement coverage**

Confirm in the diff that: the option is main-only; disabled mode has no timing call/file/output; start excludes model load; records occur after sampling and piece conversion; first ITL equals TTFT; summary includes all requested fields; and stdout code remains format-compatible.
