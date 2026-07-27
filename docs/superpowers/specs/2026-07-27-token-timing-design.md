# Token Timing JSONL 设计

## 目标

为 `examples/main/main.cpp` 增加可选的逐 token decode 阶段计时能力。用户通过
`--token-timing-file PATH` 启用后，程序将每个生成 token 写成一行 JSONL，并在正常
生成结束时追加一行 summary。未指定参数时，保持现有计时、stdout 输出和生成行为。

## 范围与非目标

- 只覆盖 `llama-cli`（`LLAMA_EXAMPLE_MAIN`）的单轮生成流程。
- 不处理多轮交互输入，也不为交互轮次重新开始计时。
- 不记录 prompt token 事件；prompt 只作为 summary 中的 token 数量。
- 不修改 llama 核心库、采样器或其他 example 的行为。
- 不把模型加载时间计入任何 timing 字段。

## 方案

### 参数与组件边界

在 `common/common.h` 的 `gpt_params` 中增加 `token_timing_file` 字符串字段，默认
为空。在 `common/arg.cpp` 中注册 `--token-timing-file PATH`，并限制为
`LLAMA_EXAMPLE_MAIN`，以复用现有参数解析和帮助信息机制，同时避免其他 example
意外接受该参数。

计时状态和 JSONL 输出逻辑放在 `examples/main/main.cpp`。不新增公共 timing API：
该功能只有 `main` 使用，局部实现可以保持改动范围小。JSON 行使用仓库已有的
`nlohmann::json`，由序列化库负责 `piece` 中引号、反斜杠、换行和控制字符的转义。

### 生命周期与计时边界

1. 模型和 context 初始化完成后，若参数非空，以覆盖方式打开目标文件；打开失败时
   输出错误并退出。
2. 完成 prompt tokenization、session 处理和生成状态初始化后，在首次处理初始
   prompt 的 decode 路径之前设置 `ggml_time_us()` 起点。文件打开、模型加载和
   参数解析都发生在起点之前。
3. 每个生成 token 在完成其对应的 decode、采样以及
   `llama_token_to_piece(ctx, id, params.special)` 后取得当前时间戳。时间戳立刻
   转为毫秒值，用于事件中的累计耗时和 ITL；文件写入本身不改变已记录的时间戳。
4. 生成循环正常结束后取得结束时间并写入 summary。每个事件写完后 flush，使文件
   可以在长时间生成过程中被消费。

为保证 timing 文件不依赖 stdout 开关，逐 token 计时路径独立于 `display_prompt`
和 console 输出路径；stdout 仍使用原有代码和格式。

### JSONL 数据格式

每个生成 token 写入：

```json
{"type":"token","index":0,"id":1234,"piece":"Hello","elapsed_ms":812.4,"itl_ms":812.4}
```

字段定义：

- `index`：从 0 开始的生成 token 序号。
- `id`：生成 token ID。
- `piece`：使用当前 stdout 规则转换出的 token 文本。
- `elapsed_ms`：当前 token 文本转换完成时刻相对 prompt 起点的累计毫秒数。
- `itl_ms`：当前 token 与前一个生成 token 的记录时刻间隔；第一个 token 使用
  自起点的累计耗时，即 TTFT。

正常结束时追加：

```json
{"type":"summary","prompt_tokens":128,"generated_tokens":64,"ttft_ms":812.4,"total_generation_ms":28123.7,"average_itl_ms":442.5}
```

summary 字段定义：

- `prompt_tokens`：初始 prompt 的 token 数量。
- `generated_tokens`：已记录的生成 token 数量。
- `ttft_ms`：第一个生成 token 的 `elapsed_ms`；没有生成 token 时为 `0`。
- `total_generation_ms`：生成流程结束时刻相对 prompt 起点的毫秒数；该值不包含
  模型加载时间。
- `average_itl_ms`：所有 token 事件 `itl_ms` 的算术平均值，包含第一个 token 的
  TTFT；没有生成 token 时为 `0`。

数值字段以 JSON number 写入，时间单位统一为毫秒，底层计时统一使用
`ggml_time_us()`。

### 错误处理与默认行为

- 未指定 `--token-timing-file` 时不创建文件、不执行额外 `ggml_time_us()` 调用、
  不增加日志或 stdout 内容。
- 指定路径无法打开时记录错误并返回非零状态，不进入生成流程。
- 事件写入沿用 `ofstream` 的错误状态检查；正常完成时只有成功进入生成流程的
  invocation 写 summary。
- 该功能不改变已有 `gpt_perf_print()`、YAML logfile 或 console 清理逻辑。

## 修改文件

- `common/common.h`：增加 `gpt_params::token_timing_file`。
- `common/arg.cpp`：增加仅属于 `LLAMA_EXAMPLE_MAIN` 的命令行选项。
- `examples/main/main.cpp`：打开 JSONL 文件、维护计时状态、写 token 事件和 summary。
- `tests/test-arg-parser.cpp`：覆盖新参数的 main 解析及默认值；保持参数去重检查。

## 验证策略

1. 参数测试验证默认值为空、`--token-timing-file PATH` 在 main example 中被接受，
   并继续通过全量参数重复检查。
2. 编译 `llama-cli`，确认新字段、选项和 JSONL writer 在 C++11 配置下可编译。
3. 使用可用的 GGUF 模型运行短生成，检查：未启用时 stdout 无新增格式；启用时每
   个 token 一行、index 连续、首 token 的 ITL 等于 TTFT、summary 数量与事件一致，
   且包含需要转义的 token 文本时文件仍是合法 JSONL。
