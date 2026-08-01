# silicon_truth_bridge

`silicon_truth_bridge`（`stb`）是面向芯片验证与 RTL 调试的只读 MCP evidence
server。它通过 Synopsys Verdi Python NPI 访问 design DB、Language/Text
metadata 和 FSDB 波形，并向 MCP client 或 CLI 提供结构化、可审计的设计与波形
证据。

`stb` 专注于证据提取，不修改 RTL、design DB 或 FSDB，也不执行 root-cause
推理。分析、补丁生成和验证策略由上层 agent 或使用者负责。

## 功能

主要能力：

- 18 个核心工具：context、wave、catalog、object、connectivity、trace、
  waveform、source、assertion、mapping、artifact。
- 6 个诊断工具：doctor、metrics、trace、logs、benchmark、selftest。
- MCP server、CLI、worker、supervisor 共享同一个 dispatcher。
- fake backend 用于契约测试和无 Verdi 环境的功能验证。
- Verdi Python NPI backend 用于真实 design DB 和 FSDB 取证。
- 多 context、多 FSDB、同 context 和跨 context waveform compare。
- generation-bound object refs、cursor refs、resource change 检测。
- `.v`、`.sv`、`.vh`、`.svh` source context 和 Text NPI macro/include 证据。
- Verdi assertion 对象锚定的受限 SVA 结构证据。
- 低开销 request metrics，区分 queue、NPI、Python、serialization、transport。

## 发布范围

Verdi、VCS、Python NPI、design DB 和 FSDB 属于用户自行准备的外部运行环境。
本仓库仅分发 STB adapter、通用测试 fixture 和项目文档，不包含 Synopsys 软件、
厂商文档、商业 IP、工程源码或项目波形数据。Python 依赖通过包管理器单独安装。

## 许可证

本仓库采用自定义的 `Personal Learning and Research License 1.0`：

- 仅允许自然人用于个人、非商业的学习和研究。
- 禁止销售、SaaS、咨询、企业内部业务、商业研发、产品集成和其他直接或间接
  商业用途。
- 修改或再分发时必须保留许可证和版权声明，并继续使用相同条款。
- 商业使用必须事先取得版权持有人的单独书面许可。

完整条款见 [LICENSE](LICENSE)。由于许可证限制商业使用，本项目属于公开源码
（source-available），不属于 OSI 定义的开源软件。

## 设计目标

`stb` 解决的问题：

- 让 agent 能读 Verdi design DB，而不是只靠源码文本猜测层次、端口、net、
  register 和 driver/load。
- 让 agent 能读 FSDB 真实波形，定位某个时间点的值、变化、状态机 timeline
  和 transaction window。
- 让 design object、source location、preprocessor evidence 和 waveform
  signal 之间可以做可审计 mapping。
- 让所有证据都有 receipt、fingerprint、generation 和 limit 信息，便于复现。
- 让 MCP 本身可调试，有 metrics、logs、benchmark 和 selftest。

非目标：

- 不解析 lint log 或 compile log。
- 不提供通用源码文件浏览器。
- 不执行任意 shell、Python 或任意 NPI method。
- 不暴露 raw NPI handle。
- 不写 FSDB 或修改 design DB。
- 不执行 assertion 仿真语义，也不判定 pass、fail、vacuity 或 root cause。
- 不内置协议级 root-cause 结论。
- 不做 fuzzy mapping，不 silent reload，不 silent truncation。
- 不在 MCP 重启后恢复 live NPI worker。

## 架构

```text
Codex / Claude Code / Human CLI
        |
        | MCP stdio or stb CLI JSON
        v
ToolDispatcher
        |
        v
Supervisor
        |
        | local launcher, one subprocess per context
        v
Worker
        |
        +-- FakeBackend
        |
        +-- VerdiBackend
              |
              +-- npisys / netlist / lang / text / waveform
              +-- design DB
              +-- FSDB
```

核心原则：

- `ToolDispatcher` 是 MCP、CLI、测试共用入口，避免 CLI 和 MCP 语义分叉。
- `Supervisor` 管理 context 生命周期、worker 进程、timeout、metrics 和 logs。
- `Worker` 隔离 Verdi NPI 全局状态。一个 context 对应一个 worker generation。
- `VerdiBackend` 是只读 adapter，负责把 NPI 对象转成稳定 JSON evidence。
- `ArtifactManager` 只向配置的 artifact root 写入 evidence artifact。
- V1 默认 launcher 是 `local`，架构保留多 launcher 扩展点。

## 安装

推荐使用 Python 3.11。

```bash
cd silicon_truth_bridge
export STB_REPO_ROOT="$PWD"
export PATH="$STB_REPO_ROOT/.venv/bin:$PATH"
python3.11 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e '.[dev]'
```

快速确认 CLI 可用：

```bash
.venv/bin/stb schema object_query
.venv/bin/stb --backend fake call context_manage --request <(printf '{"action":"list"}')
```

真实 Verdi backend 需要：

- Synopsys Verdi；当前 verified baseline 是 `V-2023.12-SP1`
- 可用 license
- `pynpi` 位于 `$STB_VERDI_HOME/share/NPI/python`
- design DB 或 RTL compile argv
- FSDB 文件
- `STB_ALLOWED_ROOTS` 覆盖 design DB、源码、FSDB 和 artifact 访问路径

默认 verified Verdi 版本是 `V-2023.12-SP1`。其他版本默认拒绝，除非显式设置：

```bash
STB_ALLOW_UNVERIFIED_VERDI=1
```

这个开关只表示允许启动，不表示 API 行为已被 V1 验证。

### Verdi 多版本策略

STB 将“可试运行”和“已验证兼容”分开：

- `V-2023.12-SP1` 已通过完整真实 Verdi/VCS 和 MCP client 回归，状态为
  `verified`。
- 其他 release 默认拒绝。设置 `STB_ALLOW_UNVERIFIED_VERDI=1` 后，worker
  会尝试导入该版本的 `pynpi`，确认模块来自所选 `STB_VERDI_HOME`，并检查
  STB 所需的 module-level NPI symbols。
- symbol probe 通过只说明接口形态基本匹配，receipt 和
  `catalog backend_capabilities` 仍会标记为 `unverified`。
- 新 release 只有在完整真实测试矩阵通过后，才应加入
  `VERIFIED_VERDI_RELEASES`。

通常只需切换安装目录：

```bash
export EDA_ROOT="${EDA_ROOT:?set EDA_ROOT to the EDA installation root}"
export PROJECT_ROOT="${PROJECT_ROOT:?set PROJECT_ROOT to the design workspace}"
export VERDI_HOME="$EDA_ROOT/verdi/V-2024.09"
export STB_VERDI_HOME="$VERDI_HOME"
export STB_ALLOW_UNVERIFIED_VERDI=1
```

如果安装目录使用 `current` 等软链接，STB 会从解析后的真实路径识别 release。
对于无法从目录名识别的自定义布局，可显式指定：

```bash
export VERDI_HOME="$EDA_ROOT/verdi/current"
export STB_VERDI_HOME="$VERDI_HOME"
export STB_VERDI_RELEASE=V-2024.09
```

不同 Verdi release 的 Python NPI 可能要求不同 Python ABI。此时应创建独立
worker 环境，并确保 STB 也安装在该环境中：

```bash
export COMPATIBLE_PYTHON="${COMPATIBLE_PYTHON:?set COMPATIBLE_PYTHON}"
export STB_WORKER_VENV="$STB_REPO_ROOT/.venv-verdi-V-2024.09"
"$COMPATIBLE_PYTHON" -m venv "$STB_WORKER_VENV"
"$STB_WORKER_VENV/bin/pip" install -e "$STB_REPO_ROOT"
export STB_WORKER_PYTHON="$STB_WORKER_VENV/bin/python"
```

MCP server 本身仍可运行在主 `.venv`；只有 NPI worker 使用
`STB_WORKER_PYTHON`。如果目标 Python 低于本项目 `requires-python`，该 release
当前不受支持，不能仅靠环境变量绕过。

一台机器需要并存多个 release 时，可在 MCP client 中注册多个
stdio server，例如 `stb-verdi-2023` 和 `stb-verdi-2024`，每个 server 分别设置
`STB_VERDI_HOME`、`STB_VERDI_RELEASE`、`STB_WORKER_PYTHON` 和
`STB_ALLOW_UNVERIFIED_VERDI`。每个 worker 进程只能加载一个 `pynpi` release。

切换 release 的完整步骤：

1. 使用 `context_manage close` 关闭当前 context。
2. 修改 MCP server 的 `STB_VERDI_HOME`；自定义目录再设置
   `STB_VERDI_RELEASE`，需要不同 Python ABI 时设置 `STB_WORKER_PYTHON`。
3. 重启 MCP client 或对应 MCP server。已经加载到 worker 进程中的
   `pynpi` 不能通过修改环境变量或 `current` 软链接动态替换。
4. 运行 `admin_doctor`，确认 `verdi_version`、`worker_python`、
   `pynpi_importable`、`pynpi_missing_symbols` 和
   `pynpi_unexpected_module_origins`。
5. 重新执行 `context_manage open`。

Codex 多版本注册示例：

```bash
export VERDI_HOME_2024="$EDA_ROOT/verdi/V-2024.09"
export STB_WORKER_VENV_2024="$STB_REPO_ROOT/.venv-verdi-V-2024.09"
export STB_ALLOWED_ROOTS="$PROJECT_ROOT:$EDA_ROOT"

codex mcp add stb-verdi-2024 \
  --env STB_BACKEND=verdi \
  --env STB_VERDI_HOME="$VERDI_HOME_2024" \
  --env STB_VERDI_RELEASE=V-2024.09 \
  --env STB_WORKER_PYTHON="$STB_WORKER_VENV_2024/bin/python" \
  --env STB_ALLOW_UNVERIFIED_VERDI=1 \
  --env STB_ALLOWED_ROOTS="$STB_ALLOWED_ROOTS" \
  -- "$STB_REPO_ROOT/.venv/bin/stb-mcp"
```

VCS 只用于生成 combined integration fixture，不是 STB MCP 运行时依赖。测试
查找顺序为 `STB_VCS`，然后是 `PATH` 中的 `vcs`。推荐从标准 `VCS_HOME`
显式派生：

```bash
export VCS_HOME="$EDA_ROOT/vcs/V-2024.09"
export STB_VCS="$VCS_HOME/bin/vcs"
```

## 配置

配置通过环境变量或 CLI 参数传入。环境变量前缀为 `STB_`。

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `STB_BACKEND` | `fake` | 默认 backend。真实使用设置为 `verdi`。 |
| `STB_LAUNCHER` | `local` | worker launcher。V1 默认只实现 local。 |
| `STB_VERDI_HOME` | `$VERDI_HOME`，否则未设置 | Verdi 安装路径；Verdi backend 必填。 |
| `STB_VERDI_RELEASE` | 自动识别 | 自定义安装布局下显式声明 release。 |
| `STB_WORKER_PYTHON` | MCP server 当前 Python | 启动 NPI worker 的 Python。 |
| `STB_ALLOW_UNVERIFIED_VERDI` | `false` | 允许未验证 release 通过 capability probe 后试运行。 |
| `STB_ALLOWED_ROOTS` | 当前目录 | 冒号分隔的允许访问根目录。 |
| `STB_DEV_TOOLS` | `false` | 是否注册 admin_* 开发工具。 |
| `STB_MAX_ACTIVE_CONTEXTS` | `4` | 最大 active worker/context 数。 |
| `STB_MAX_OBJECT_HANDLES` | `100000` | language object handle 保留上限。 |
| `STB_DEFAULT_TIMEOUT_SEC` | `120` | worker request soft deadline。 |
| `STB_HARD_TIMEOUT_SEC` | `300` | supervisor hard timeout。 |
| `STB_NORMAL_RESPONSE_BYTES` | `4194304` | 普通响应大小限制。 |
| `STB_HARD_RESPONSE_BYTES` | `16777216` | 硬响应大小限制。 |
| `STB_ARTIFACT_ROOT` | `.stb/artifacts` | artifact 输出目录。 |
| `STB_MAX_ARTIFACT_BYTES` | `1073741824` | 单 artifact 上限。 |
| `STB_MAX_ARTIFACT_TOTAL_BYTES` | `21474836480` | artifact 总量上限。 |

示例：

```bash
export EDA_ROOT="${EDA_ROOT:?set EDA_ROOT to the EDA installation root}"
export PROJECT_ROOT="${PROJECT_ROOT:?set PROJECT_ROOT to the design workspace}"
export VERDI_HOME="${VERDI_HOME:-$EDA_ROOT/verdi/V-2023.12-SP1}"
export STB_BACKEND=verdi
export STB_VERDI_HOME="$VERDI_HOME"
export STB_ALLOWED_ROOTS="$PROJECT_ROOT:$EDA_ROOT"
export STB_ARTIFACT_ROOT="$PROJECT_ROOT/.stb/artifacts"
export STB_DEV_TOOLS=1
```

## CLI 使用

`stb` CLI 和 MCP 使用同一套 tool request schema。

### 查看工具 schema

```bash
.venv/bin/stb schema context_manage
.venv/bin/stb schema object_query
.venv/bin/stb schema wave_changes
.venv/bin/stb schema mapping
```

### 一次性调用

One-shot 调用适合 `schema`、`admin_doctor`、`context_manage list` 等不需要保留
worker 生命周期的操作。

```bash
printf '{"action":"list"}' | \
  .venv/bin/stb --backend fake --pretty call context_manage
```

One-shot 进程退出时 supervisor 会关闭 context，因此连续的 open/query 工作流
应使用 `shell` 或 `batch`。

### 交互 shell

```bash
STB_BACKEND=verdi \
STB_ALLOWED_ROOTS="$PROJECT_ROOT:$EDA_ROOT" \
.venv/bin/stb --pretty shell
```

每行输入一个 JSON command：

```json
{"tool":"context_manage","request":{"action":"list"}}
```

输入 `quit` 或 `exit` 退出。

### JSONL batch

batch 模式在一个 supervisor 生命周期里执行多条命令，适合真实 DB/FSDB。

```bash
.venv/bin/stb \
  --backend verdi \
  --allowed-roots "$PROJECT_ROOT:$EDA_ROOT" \
  --pretty \
  batch workflow.jsonl
```

`workflow.jsonl` 每行是一条 command。以下 `${...}` 是文档占位符；JSON
不会自动展开 shell 变量，调用方应在生成请求时替换为实际值：

```jsonl
{"tool":"context_manage","request":{"action":"open","context_id":"demo","backend":"verdi","design_spec":{"argv":["-dbdir","${SIM_DB}"]},"wave_specs":[{"wave_id":"run","path":"${WAVE_FILE}"}]}}
{"tool":"object_query","request":{"context_id":"demo","request":{"model":"netlist","scope":"top","npi_types":["INST"],"limit":20,"max_scan":5000}}}
{"tool":"wave_value","request":{"context_id":"demo","request":{"wave_id":"run","signals":["top.clk"],"times":["100ns","1us"]}}}
{"tool":"context_manage","request":{"action":"close","context_id":"demo"}}
```

Exit status:

- `0`: 所有请求 complete
- `2`: 请求 failed 或 CLI/worker 失败
- `3`: 至少一个请求 partial

调试选项：

```bash
--pretty
--receipt-only
--output-file response.json
--save-request saved-request.json
--save-response saved-response.json
```

### Replay

`replay` 执行保存过的 command JSON：

```bash
.venv/bin/stb --pretty replay saved-request.json
```

示例文件：

```json
{
  "tool": "context_manage",
  "request": {"action": "list"}
}
```

## MCP 使用

启动 stdio MCP server：

```bash
STB_BACKEND=verdi \
STB_VERDI_HOME="$VERDI_HOME" \
STB_ALLOWED_ROOTS="$PROJECT_ROOT:$EDA_ROOT" \
STB_DEV_TOOLS=1 \
stb-mcp
```

在 Codex、Claude Code 或其他 MCP client 中，将 `silicon_truth_bridge` 配成 stdio
server。概念上需要传入以下内容：

```json
{
  "command": "stb-mcp",
  "args": [],
  "envFrom": [
    "STB_BACKEND",
    "STB_VERDI_HOME",
    "STB_ALLOWED_ROOTS",
    "STB_DEV_TOOLS"
  ]
}
```

上面的 `envFrom` 是表示 client 环境继承关系的伪字段。实际字段由 MCP client
定义；`stb-mcp` 仅使用 stdio，不启动 HTTP daemon。

### Codex 配置

用 Codex CLI 注册用户级 stdio MCP：

```bash
codex mcp add stb \
  --env STB_BACKEND=verdi \
  --env STB_VERDI_HOME="$VERDI_HOME" \
  --env VERDI_HOME="$VERDI_HOME" \
  --env STB_ALLOWED_ROOTS="$PROJECT_ROOT:$EDA_ROOT" \
  --env STB_ARTIFACT_ROOT="$PROJECT_ROOT/.stb/artifacts" \
  -- "$STB_REPO_ROOT/.venv/bin/stb-mcp"
```

Verdi license 和动态库环境应由启动 Codex 的 shell 提供。可在
`${CODEX_HOME:-$HOME/.codex}/config.toml` 中转发变量，并为只读 STB tools
设置审批策略：

```toml
[mcp_servers.stb]
command = "stb-mcp"
env_vars = [
  "SNPSLMD_LICENSE_FILE",
  "LM_LICENSE_FILE",
  "LD_LIBRARY_PATH",
  "PATH",
  "STB_BACKEND",
  "STB_VERDI_HOME",
  "VERDI_HOME",
  "STB_ALLOWED_ROOTS",
  "STB_ARTIFACT_ROOT"
]
startup_timeout_sec = 30
tool_timeout_sec = 300
default_tools_approval_mode = "approve"
```

检查连接：

```bash
codex mcp list
```

### Claude Code 配置

使用 Claude Code CLI 注册用户级 stdio MCP：

```bash
claude mcp add --scope user --transport stdio \
  stb \
  --env STB_BACKEND=verdi \
  --env STB_VERDI_HOME="$VERDI_HOME" \
  --env VERDI_HOME="$VERDI_HOME" \
  --env STB_ALLOWED_ROOTS="$PROJECT_ROOT:$EDA_ROOT" \
  --env STB_ARTIFACT_ROOT="$PROJECT_ROOT/.stb/artifacts" \
  -- "$STB_REPO_ROOT/.venv/bin/stb-mcp"

claude mcp list
```

Claude Code 进程需要从启动 shell 继承 Verdi license 和动态库环境。

## 请求格式

`context_manage`、`wave_manage`、`catalog`、`artifact` 使用直接 request：

```json
{
  "tool": "context_manage",
  "request": {
    "action": "open",
    "context_id": "rtl",
    "backend": "verdi",
    "design_spec": {"argv": ["-dbdir", "${SIM_DB}"]},
    "wave_specs": [{"wave_id": "run", "path": "${WAVE_FILE}"}]
  }
}
```

其他 worker 工具使用 wrapped request：

```json
{
  "tool": "object_query",
  "request": {
    "context_id": "rtl",
    "request": {
      "model": "netlist",
      "scope": "top.dut",
      "npi_types": ["INST"],
      "limit": 100
    }
  }
}
```

CLI 也支持把 `context_id` 和 payload 展平，但推荐 wrapped request，便于和 MCP
schema 对齐。

## 核心工具

| Tool | 作用 | 典型用途 |
|---|---|---|
| `context_manage` | 管理 evidence context | open/reload/close/status/list/release_objects |
| `wave_manage` | 管理 context 内 FSDB | attach/reload/detach/status/list |
| `catalog` | 发现 backend 能力 | models、relations、properties、operators |
| `object_resolve` | 精确解析对象 | module/instance/net/port/wave signal |
| `object_get` | 批量读取属性 | type、direction、range_size、source 等 |
| `object_query` | 有界对象查询 | list instance/register/net/wave signal |
| `object_traverse` | 沿固定 relation 遍历 | children、ports、drivers、loads、members |
| `connectivity_direct` | 一跳 driver/load | 快速确认 net 的直接驱动和负载 |
| `trace` | 有界连接图 | driver/load/path/fanin/fanout evidence graph |
| `trace_active_driver` | 时间点 active branch evidence | 结合 FSDB 判断分支是否 active |
| `trace_value_origin` | 时间点 value origin evidence | 跨 sequential boundary 的采样证据 |
| `wave_value` | 精确时间采样 | 多 signal、多 time value_at |
| `wave_changes` | 波形变化分页 | 大窗口 transition 取证 |
| `wave_compute` | 机械波形计算 | find/statistics/period/xz/IR event/transactions |
| `source_context` | NPI object 锚定源码片段 | 限定行数源码和 macro/include evidence |
| `assertion_structure` | assertion 结构证据 | 时钟、disable、implication、符号化 cycle window |
| `mapping` | design object 到 waveform signal 映射 | resolve/validate/explain |
| `artifact` | 长结果导出 | cursor 自动续读并写 artifact |

### 诊断工具

诊断工具默认不注册，必须设置 `STB_DEV_TOOLS=1` 或 CLI `--dev-tools`。

| Tool | 作用 |
|---|---|
| `admin_doctor` | 检查 runtime、Verdi NPI、worker、路径和资源可用性 |
| `admin_metrics` | 查看、重置、导出或比较 metrics |
| `admin_trace` | 配置和读取 request trace |
| `admin_logs` | 读取 bounded supervisor/worker/native logs |
| `admin_benchmark` | 运行受限 benchmark |
| `admin_selftest` | 运行 transport/backend/design/waveform 自检 |

示例：

```bash
printf '{}' | .venv/bin/stb --dev-tools --pretty call admin_doctor

printf '{"action":"snapshot"}' | \
  .venv/bin/stb --dev-tools --pretty call admin_metrics
```

## 常用工作流

### 打开 Verdi design DB 和 FSDB

```json
{
  "tool": "context_manage",
  "request": {
    "action": "open",
    "context_id": "case1",
    "backend": "verdi",
    "design_spec": {
      "argv": ["-dbdir", "${SIM_DB}"]
    },
    "wave_specs": [
      {"wave_id": "run", "path": "${WAVE_FILE}"}
    ]
  }
}
```

也可以只打开 design DB：

```json
{
  "tool": "context_manage",
  "request": {
    "action": "open",
    "context_id": "rtl",
    "backend": "verdi",
    "design_spec": {
      "argv": ["-sv", "top.sv", "-top", "top"]
    }
  }
}
```

也可以只打开 FSDB：

```json
{
  "tool": "context_manage",
  "request": {
    "action": "open",
    "context_id": "waves",
    "backend": "verdi",
    "wave_specs": [
      {"wave_id": "run0", "path": "${WAVE_A}"},
      {"wave_id": "run1", "path": "${WAVE_B}"}
    ]
  }
}
```

### 查询 instance、register、net、port

列 instance：

```json
{
  "tool": "object_query",
  "request": {
    "context_id": "case1",
    "request": {
      "model": "netlist",
      "scope": "top.dut",
      "npi_types": ["INST"],
      "limit": 50,
      "max_scan": 50000
    }
  }
}
```

列 register：

```json
{
  "tool": "object_query",
  "request": {
    "context_id": "case1",
    "request": {
      "model": "netlist",
      "scope": "top.dut.u_block",
      "semantic_classes": ["register"],
      "limit": 50,
      "max_scan": 100000
    }
  }
}
```

按名字筛选：

```json
{
  "tool": "object_query",
  "request": {
    "context_id": "case1",
    "request": {
      "model": "netlist",
      "scope": "top.dut",
      "npi_types": ["INST"],
      "where": {"op": "glob", "property": "name", "value": "*ltssm*"},
      "limit": 20
    }
  }
}
```

### 查 driver/load

```json
{
  "tool": "connectivity_direct",
  "request": {
    "context_id": "case1",
    "request": {
      "kind": "driver",
      "signals": ["top.dut.u_block.state_q"]
    }
  }
}
```

有界 trace：

```json
{
  "tool": "trace",
  "request": {
    "context_id": "case1",
    "request": {
      "kind": "fanin",
      "roots": ["top.dut.u_block.state_q"],
      "max_depth": 8,
      "max_nodes": 1000
    }
  }
}
```

### 读波形值

```json
{
  "tool": "wave_value",
  "request": {
    "context_id": "case1",
    "request": {
      "wave_id": "run",
      "signals": [
        "top.dut.clk",
        "top.dut.u_block.state_q"
      ],
      "times": ["100ns", "1us", "865.00266493us"]
    }
  }
}
```

### 分页读取波形变化

```json
{
  "tool": "wave_changes",
  "request": {
    "context_id": "case1",
    "request": {
      "wave_id": "run",
      "signals": ["top.dut.u_block.state_q"],
      "start": "0fs",
      "end": "2ms",
      "direction": "forward",
      "max_changes": 1000
    }
  }
}
```

如果返回 `status=partial`，继续传入 `data.next_cursor`：

```json
{
  "tool": "wave_changes",
  "request": {
    "context_id": "case1",
    "request": {
      "wave_id": "run",
      "signals": ["top.dut.u_block.state_q"],
      "start": "0fs",
      "end": "2ms",
      "direction": "forward",
      "max_changes": 1000,
      "cursor": "cur-..."
    }
  }
}
```

### 机械波形计算

统计 transition 和 X/Z：

```json
{
  "tool": "wave_compute",
  "request": {
    "context_id": "case1",
    "request": {
      "operation": "statistics",
      "wave_id": "run",
      "signals": ["top.dut.u_block.state_q"],
      "start": "0fs",
      "end": "2ms"
    }
  }
}
```

基于表达式 IR 提取事件：

```json
{
  "tool": "wave_compute",
  "request": {
    "context_id": "case1",
    "request": {
      "operation": "extract_events",
      "wave_id": "run",
      "start": "0fs",
      "end": "2ms",
      "edge": "posedge",
      "expression": {
        "expr_version": "stb.expr.v1",
        "root": {
          "op": "logic.eq",
          "args": [
            {"signal": "top.dut.valid"},
            {"literal": "1'b1"}
          ]
        }
      }
    }
  }
}
```

跨 context 或跨 FSDB 比较：

```json
{
  "tool": "wave_compute",
  "request": {
    "context_id": "left_ctx",
    "request": {
      "operation": "first_divergence",
      "context_mode": "cross",
      "left": {
        "context_id": "left_ctx",
        "wave_id": "run",
        "signal": "top.dut.state_q"
      },
      "right": {
        "context_id": "right_ctx",
        "wave_id": "run",
        "signal": "top.dut.state_q"
      },
      "start": "0fs",
      "end": "2ms"
    }
  }
}
```

### Source context 和宏证据

先用 `object_query` 或 `object_resolve` 拿到 `ref`，再请求源码上下文：

```json
{
  "tool": "source_context",
  "request": {
    "context_id": "case1",
    "request": {
      "reference": {
        "model": "netlist",
        "context_id": "case1",
        "worker_generation": 1,
        "npi_type": "INST",
        "full_name": "top.dut.u_block.state_q"
      },
      "before_lines": 5,
      "after_lines": 5,
      "max_lines": 80,
      "include_preprocessor": true
    }
  }
}
```

`include_preprocessor=true` 会使用 Text NPI 收集相关 macro/include evidence。
它是 lazy 的，因为大型预编译 DB 的第一次 Text metadata 查询可能比较慢。

### Assertion 结构证据

先通过 `object_query` 获取 Verdi assertion `ObjectRef`：

```json
{
  "tool": "object_query",
  "request": {
    "context_id": "case1",
    "request": {
      "model": "language",
      "scope": "top",
      "npi_types": ["npiAssert"],
      "limit": 100
    }
  }
}
```

再将返回的 `ref` 传给 `assertion_structure`。该工具使用 Verdi NPI 确认
assertion 身份、源码锚点和命名 property 关联，然后只对锚定源码执行受限解析。

首版支持显式 `posedge`/`negedge` clock、`disable iff`、`|->`、`|=>`、
固定或范围 `##`，以及 `$past`、`$rose`、`$fell`、`$stable` 的结构化记录。
`##[m:n]` 保持符号化窗口，不展开执行路径。

`structure.fidelity` 分别报告 syntax、temporal 和 dependency fidelity。
遇到未支持的高级 sequence 语义时，工具保留对象、源码和原始表达式，但不生成
部分 timeline。宏叶子保留为 `opaque`，标识符只有经过 NPI 精确解析后才成为
signal evidence。输出不结合 FSDB 判断 assertion 的 pass、fail 或 vacuity。

可用性由当前 context 的运行时 probe 决定，可通过
`catalog(kind="backend_capabilities")` 查看。未通过对象发现和源码锚定检查时，
工具返回 `unsupported_capability`，不会退化为任意文件扫描。

### Design 到 waveform mapping

同 context 默认尝试固定、可解释的 deterministic rules：

```json
{
  "tool": "mapping",
  "request": {
    "context_id": "case1",
    "request": {
      "action": "resolve",
      "context_mode": "same",
      "wave_id": "run",
      "design_full_name": "top.dut.u_block.state_q"
    }
  }
}
```

跨 context 必须提供 explicit profile，不做 fuzzy guess：

```json
{
  "tool": "mapping",
  "request": {
    "context_id": "left_ctx",
    "request": {
      "action": "resolve",
      "context_mode": "cross",
      "wave_id": "run",
      "design_full_name": "tb.dut/state_q",
      "profile": {
        "rules": [
          {
            "kind": "separator_normalize",
            "source": "/",
            "target": "."
          },
          {
            "kind": "prefix_replace",
            "source_prefix": "tb.",
            "target_prefix": "top."
          }
        ]
      }
    }
  }
}
```

### Artifact 导出

长结果应导出为 artifact，而不是一次性塞进 MCP response。

```json
{
  "tool": "artifact",
  "request": {
    "action": "export",
    "request": {
      "context_id": "case1",
      "method": "wave_changes",
      "args": {
        "wave_id": "run",
        "signals": ["top.dut.u_block.state_q"],
        "start": "0fs",
        "end": "2ms",
        "direction": "forward",
        "max_changes": 10000
      }
    }
  }
}
```

查询 job：

```json
{
  "tool": "artifact",
  "request": {
    "action": "status",
    "request": {"job_id": "job-..."}
  }
}
```

## 对象模型

V1 暴露三类 model：

- `netlist`: Verdi Netlist NPI 对象，典型类型包括 `INST`、`PORT`、
  `INSTPORT`、`DECL_NET`、`CONCAT_NET`、`SLICE_NET`。
- `language`: Verdi Language NPI 对象，典型语义包括 assignment、
  continuous assignment、event control、if/case statement、expression。
- `waveform`: FSDB waveform object，典型类型为 `SCOPE` 和 `SIGNAL`。

常用 semantic class：

- `module_instance`
- `register`
- `latch`
- `memory`
- `combinational_net`
- `assignment`
- `continuous_assignment`
- `event_control`
- `if_statement`
- `case_statement`
- `expression`
- `waveform_scope`
- `waveform_signal`

使用 `catalog` 查看当前 backend 的完整可用集合：

```json
{
  "tool": "catalog",
  "request": {
    "context_id": "case1",
    "kind": "semantic_classes"
  }
}
```

## 响应、回执与限制

典型响应：

```json
{
  "status": "complete",
  "data": {},
  "receipt": {
    "api_version": "stb.v1",
    "request_id": "req-...",
    "context_id": "case1",
    "worker_generation": 1,
    "backend": "python_npi",
    "design_fingerprint": "sha256:...",
    "wave_id": "run",
    "wave_generation": 1,
    "verdi_version": "V-2023.12-SP1",
    "limits": {
      "truncated": false
    },
    "metrics": {
      "duration_ms": 10.0,
      "total_ms": 10.0,
      "queue_ms": 0.0,
      "npi_ms": 8.0,
      "python_ms": 1.0,
      "serialization_ms": 0.2,
      "transport_ms": 0.8,
      "input_bytes": 100,
      "response_bytes": 1000,
      "npi_calls": 10,
      "cache_hits": 0,
      "cache_misses": 1
    }
  }
}
```

`status` 语义：

- `complete`: 请求完整完成。
- `partial`: 达到 cursor、node、transition、response 或 timeout limit，需要继续取证。
- `failed`: 请求失败，`error.code` 是机器可读原因。

所有分页 cursor 都绑定 context、worker generation、wave generation 和请求 key。
reload 后的旧 cursor/ref 会被拒绝。

## 资源与 Generation 语义

Context：

- `context_manage open` 创建 worker generation。
- `context_manage reload` 会关闭旧 worker 并创建新 generation。
- `ObjectRef` 必须携带 `context_id` 和 `worker_generation`。
- 旧 generation 的 object ref 使用时返回 stale error。

Design resource：

- design DB 或非源码设计资源变化会冻结当前 design evidence。
- 需要显式 `context_manage reload` 重新加载。

Wave resource：

- FSDB 变化只影响对应 wave。
- 需要显式 `wave_manage reload` 重新打开。
- reload 后 `wave_generation` 增加，旧 wave cursor 失效。

Source resource：

- 源码变化不会冻结整个 context。
- `source_context` 会报告 `source_alignment: stale` 或 `aligned`。
- 未设置 `allow_current_changed_source=true` 时，变化源码不会被静默当作原始 evidence。

## 性能

已实现的性能优化：

- context 懒启动，worker 按需打开。
- active worker 上限，防止误开大量 Verdi 进程。
- query result cache、mapping cache、wave signal handle cache。
- assertion handle 仅在对应 worker generation 内保持有效。
- range delay 保持符号化，不进行路径枚举。
- netlist/waveform 稳定 handle property cache。
- instance children/local object LRU cache。
- `netlist.get_inst` 作为 instance scope resolve fast path。
- register/module semantic class 短路匹配。
- 无 cursor `wave_changes` exact-query LRU，命中时重新生成 cursor。
- receipt 中记录 `npi_ms` 和 `npi_calls`，用于区分 Verdi NPI 和 Python 包装层耗时。

基准文件：

- [fake-baseline.json](benchmarks/fake-baseline.json)

重新生成：

```bash
.venv/bin/python scripts/run_benchmarks.py \
  --output benchmarks/fake-baseline.json
```

性能边界：

- `wave_value_cold` 主要受 FSDB `sig_by_name` 冷查找影响。
- register cold query 主要受 Verdi NPI 层次扫描和 `cell_type` 查询影响。
- Python 包装层当前不是主要瓶颈。
- 要继续大幅优化 cold path，需要 V2 方案，例如 C/C++ NPI helper、预构建
  register index、FSDB signal index 预热或持久化索引。

## 测试

Hermetic tests：

```bash
.venv/bin/pytest -q
```

真实 Verdi NPI tests：

```bash
STB_RUN_REAL_NPI=1 .venv/bin/pytest -q
```

静态检查：

```bash
.venv/bin/python -m py_compile src/stb/*.py src/stb/backends/*.py
.venv/bin/stb schema wave_compute
.venv/bin/stb schema mapping
```

## 目录结构

```text
src/stb/
  server.py              MCP stdio server
  cli.py                 human and automation CLI
  dispatcher.py          shared public tool dispatcher
  supervisor.py          context and worker lifecycle
  worker.py              subprocess worker protocol endpoint
  schemas.py             strict public request schemas
  models.py              response, receipt, value, ref models
  artifacts.py           artifact and async job manager
  response_limits.py     response size enforcement
  backends/
    base.py              backend interface
    fake.py              deterministic test backend
    verdi.py             Verdi Python NPI backend

docs/
  stb-v1-architecture.md
  stb-v1-api-contract.md
  stb-v1-cli.md
  stb-v1-verification-plan.md

scripts/
  run_benchmarks.py

tests/
  hermetic and real NPI integration tests

benchmarks/
  fake-baseline.json
```

## 故障排查

### `pynpi` import 失败

检查：

```bash
ls "$STB_VERDI_HOME/share/NPI/python"
printf '{}' | .venv/bin/stb --dev-tools --pretty call admin_doctor
```

确保 `STB_VERDI_HOME` 指向 `V-2023.12-SP1`。

### `unsupported_api_version`

该错误可能表示 release 未验证、`pynpi` 无法被所选 worker Python 导入，或
required NPI symbols 不完整。先运行：

```bash
printf '{}' | .venv/bin/stb --dev-tools --pretty call admin_doctor
```

当前 verified baseline 为 `V-2023.12-SP1`。其他版本可通过以下配置进行
capability probe：

```bash
export STB_ALLOW_UNVERIFIED_VERDI=1
```

如果错误 details 包含 `worker_python`，检查 Python ABI；如果包含
`unexpected_module_origins`，检查 `PYTHONPATH` 和 Verdi 环境是否混入另一版
`pynpi`；如果包含 `missing_symbols`，该 release 需要单独 adapter。未验证
release 的 receipt 会保留 `unverified` 状态。

### `source_outside_allowed_roots`

把源码、DB、FSDB 和 artifact root 的共同上层目录加入：

```bash
export STB_ALLOWED_ROOTS="$PROJECT_ROOT:$EDA_ROOT"
```

### `resource_changed` 或 `source_changed`

- design DB 变化：执行 `context_manage reload`。
- FSDB 变化：执行 `wave_manage reload`。
- source 变化：重新请求 `source_context`，必要时设置
  `allow_current_changed_source=true`，但 response 会标记 stale/current 状态。

### `cursor_expired`

cursor 是短生命周期、generation-local 的。重新执行原始 query，拿新的
`next_cursor`。

### `response_byte_limit`

降低 `limit`、`max_changes`、`max_nodes`，或改用 `artifact export`。

### Verdi native log 很大

Verdi 可能写 `npiLog/`。STB worker 自己的结构化日志在
`.stb/logs/<context_id>.log` 附近，由 supervisor 创建。

## 安全边界

`stb` 是 read-only evidence tool：

- 只读 design DB、FSDB、NPI metadata 和被 NPI object 锚定的源码上下文。
- 所有写入限制在 artifact/log/benchmark 输出。
- 不提供任意路径源码读取。
- 不提供任意 NPI method 调用。
- 不提供 shell/Python 执行。
- 不做 hidden reload。
- 不丢弃 truncation 信息。

更多上下文可通过 `catalog`、`object_query`、`object_traverse`、
`wave_changes`、`source_context`、`assertion_structure` 和 `mapping`
等结构化工具获取。

## 路线图

后续版本可扩展：

- C/C++ NPI helper，绕过 Python binding 未暴露或过慢的 list/register API。
- 可选预热索引：register index、wave signal index、scope subtree index。
- 更细粒度 sampled NPI spans。
- 更多 waveform mechanical computation 模板。
- 更完整的 language/source macro expansion evidence。
- 与项目 lint/compile log 的独立 evidence tool 集成，但不混入 STB V1 core。

### LSF 远程 launcher

当前仅支持 `STB_LAUNCHER=local`。LSF launcher 尚未实现。

STB worker 是长生命周期的双向 JSONL 协议进程，因此 LSF 集成需要管理 job
生命周期、队列等待和远程通信：

- 使用结构化配置生成 `bsub` argv，不接受任意 shell command string。
- 提交长生命周期 worker，保存 job ID，并通过 `bjobs` 跟踪
  `PEND/RUN/DONE/EXIT`。
- 将 queue wait/startup timeout 与 STB tool hard timeout 分开。
- worker 通过 authenticated reverse TCP 或 shared-filesystem handshake
  连接 Supervisor，不依赖普通 batch stdout 承载长期 JSONL。
- context close、worker crash 或 timeout 后使用 `bkill` 清理残留 job。
- receipt、status、metrics 和 logs 记录 launcher、queue、job ID、execution
  host 和 queue wait 时间。

预期配置：

```text
STB_LAUNCHER=lsf
STB_LSF_QUEUE=<queue>
STB_LSF_PROJECT=<project-or-account>
STB_LSF_APP_PROFILE=<application-profile>
STB_LSF_RESOURCE=<resource-expression>
STB_LSF_SLOTS=<slots>
STB_LSF_JOB_START_TIMEOUT_SEC=<queue-wait-timeout>
STB_LSF_BSUB=<bsub-path>
STB_LSF_BJOBS=<bjobs-path>
STB_LSF_BKILL=<bkill-path>
STB_LSF_TRANSPORT=reverse_tcp|shared_fs
```

环境要求：

- `bqueues -w` 和目标 queue 的 `bqueues -l <queue>` 信息。
- queue、project/account、application profile 和 resource requirements。
- CPU slot、memory、runtime limit，以及是否允许长时间 interactive job。
- 登录节点与计算节点之间的网络连通规则。
- 双方可访问的 shared filesystem 路径。
- 计算节点加载 Verdi、Python、动态库和 license 环境的标准方式。
- `bsub`、`bjobs`、`bqueues`、`bkill` 的实际路径和版本。
