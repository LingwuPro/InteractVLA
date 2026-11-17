<div align="center">

<img src=".github/assets/interactvla-logo.png" alt="InteractVLA" width="460">

### Turning VLAs into Interactive Tools for Agentic Robotic Manipulation

**Specify a target. Generate a motion. Inspect and refine.**

[![Project page](https://img.shields.io/badge/Project_Page-Visit-008e89?style=flat-square)](https://lingwupro.github.io/InteractVLA/)
[![Paper](https://img.shields.io/badge/Paper-OpenReview-172f46?style=flat-square)](https://openreview.net/forum?id=ytXuX6Zm6J)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)](pyproject.toml)
[![Backends](https://img.shields.io/badge/Backends-PyTorch_%7C_JAX-008e89?style=flat-square)](#backends)

[Quick start](#quick-start) · [Method](#method) · [Results](#results) · [Code map](#code-map) · [Tool API](#tool-api) · [中文技术文档](docs/implementation-guide.zh-CN.md)

</div>

---

**InteractVLA turns a frozen vision–language–action policy into an interactive tool for an agent.** The agent specifies an end-effector target; test-time geometric guidance steers the policy in its native action space. The generated action chunk then becomes feedback for the agent to inspect, revise, and execute.

This repository contains the core runtime, PyTorch and JAX guidance implementations, model adapters, evaluation integrations, and SandboxRSI resources. The [project page](https://lingwupro.github.io/InteractVLA/) includes the method overview, results, and 85 demonstration videos.

## Method

<p align="center">
  <img src=".github/assets/method-framework.png" alt="InteractVLA framework: geometric guidance and action-informed agentic refinement" width="1000">
</p>

| Component | What it does | Start reading |
| :--- | :--- | :--- |
| **Geometric guidance** | Differentiates a complete-pose objective through the current action expert while keeping model weights frozen. | [sampling.py](code/interactvla/sampling.py), [geometry.py](code/interactvla/geometry.py) |
| **Soft-time alignment** | Assigns shared arrival-time weights to position, orientation, and gripper error without fixing a target step. | [temporal_objective](code/interactvla/sampling.py), [JAX implementation](code/interactvla/jax_backend.py) |
| **Action-informed refinement** | Exposes action chunks, command poses, history, and camera projections for target revision and motion editing. | [feedback.py](code/interactvla/feedback.py), [runtime.py](code/interactvla/runtime.py) |

The default configuration uses 10 Euler evaluations, with input gradients at generation times `0.5`, `0.6`, and `0.7`. Compatibility checks can return control to the agent before a correction is applied. Compatibility measures target–policy agreement; it is not a task-success probability.

## Results

Results reproduced from the [project website](https://lingwupro.github.io/InteractVLA/#results). The tables retain the website’s values and evaluation notes; reference numbers refer to the [paper](https://openreview.net/forum?id=ytXuX6Zm6J). **Bold** marks InteractVLA results.

### LIBERO-Plus

RSI comparison on LIBERO-Plus (%). Standalone VLA results are from [8]; agent baselines marked † are from RV-ICL [2] on the stratified 120-variant subset using GPT-6 Astra. All methods follow their official evaluation protocols.

| Method | Planner | Background | Light | Language | Noise | Layout | Camera | Robot Init. | Overall ↑ |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **InteractVLA + RSI** | **GPT-6.1 Sol** | **100.0** | **100.0** | **99.9** | **98.4** | **99.2** | **99.5** | **98.9** | **99.4** |
| RV-ICL† | GPT-6 Astra | 100.0 | 94.1 | 100.0 | 100.0 | 88.2 | 94.4 | 94.1 | 95.8 |
| HarnessVLA† | GPT-6 Astra | 88.2 | 76.5 | 100.0 | 94.1 | 82.4 | 83.3 | 82.4 | 86.7 |
| OpenVLA-OFT | – | 93.3 | 88.7 | 79.5 | 75.8 | 74.2 | 56.4 | 31.9 | 69.6 |
| RIPT-VLA | – | 91.6 | 88.4 | 77.6 | 73.5 | 74.2 | 55.2 | 31.2 | 68.4 |
| π₀-FAST | – | 73.2 | 73.2 | 61.0 | 74.4 | 68.8 | 65.1 | 21.6 | 61.6 |
| π₀ | – | 81.4 | 85.0 | 58.8 | 79.0 | 68.9 | 13.8 | 6.0 | 53.6 |
| UniVLA | – | 81.0 | 69.0 | 69.6 | 21.2 | 31.9 | 1.8 | 46.2 | 42.9 |
| NORA | – | 58.6 | 45.7 | 65.1 | 12.8 | 62.1 | 2.2 | 37.0 | 39.0 |
| WorldVLA | – | 17.1 | 43.7 | 41.6 | 10.9 | 38.0 | 0.1 | 27.9 | 25.0 |
| OpenVLA | – | 34.8 | 8.1 | 23.0 | 15.2 | 28.5 | 0.8 | 3.5 | 15.6 |

**Evaluation notes.** Standalone VLA results are from [8]. † Agent baselines are from RV-ICL [2] on the stratified 120-variant subset using GPT-6 Astra. All methods follow their official evaluation protocols. Added standalone baselines come from LIBERO-Plus, Table 2 (arXiv v3). Total is shown as Overall. Selected comparisons, not the complete leaderboard; ranks apply only to the methods shown. Sources accessed November 15, 2026.

Baseline source: [LIBERO-Plus paper · Table 2](https://arxiv.org/html/2510.13626v3#S6.T2).

### RoboDojo

RSI comparison on RoboDojo (%). We compare published robot agents [26] with our Astra-based InteractVLA + RSI system under official leaderboard protocols.

| Method | Type | Generalization | Precision | Long-Horizon | Memory | Open | Overall ↑ |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| **InteractVLA + RSI** | **Agent+VLA** | **52.78** | **39.58** | **45.83** | **63.89** | **41.67** | **48.62** |
| HexaShell-Preview | Agent | 34.50 | 32.00 | 31.50 | 81.00 | 55.25 | 46.85 |
| WorldScape-R²SI | Agent+WAM | 35.83 | 15.50 | 34.25 | 63.67 | 47.50 | 39.35 |
| EmbodiedEvo | Agent | 30.83 | 16.25 | 21.75 | 76.67 | 49.25 | 38.95 |
| WALL-OSS-1.0-lite | VLA | 37.94 | 29.08 | 32.25 | 56.00 | 19.58 | 34.97 |
| VPP2 | WAM | 29.50 | 35.17 | 37.00 | 55.56 | 4.08 | 32.26 |
| PhysicalRSI | Agent+VLA | 15.62 | 32.50 | 37.33 | 46.56 | 24.92 | 31.38 |
| Awomo-0.5 | WAM | 23.84 | 33.50 | 26.75 | 41.22 | 22.92 | 29.64 |
| Simate-beta | VLA | 27.95 | 26.92 | 43.42 | 33.00 | 8.50 | 27.96 |
| Rex-M1 Preview | VLA | 26.06 | 23.83 | 33.00 | 48.22 | 6.08 | 27.44 |
| GPT-6 Astra | Agent | 30.50 | 4.00 | 8.25 | 38.67 | 31.00 | 22.48 |

**Evaluation notes.** Published robot agents [26] are compared with the Astra-based InteractVLA + RSI system under official leaderboard protocols. Added policy baselines use the official simulation leaderboard’s Success Rate, not Score. Generalization is the mean of Standard and Randomized success rates, rounded to two decimals. Overall retains the reported all-dimension average. Selected comparisons, not the complete leaderboard; ranks apply only to the methods shown. Sources accessed November 15, 2026.

Baseline source: [RoboDojo official leaderboard](https://robodojo-benchmark.com/leaderboard?bench=sim).

### RoboCasa365

RSI comparison on RoboCasa365 (%). Published VLA and agent baselines are from [25, 36]. † HexaShell 1.5 uses the pretrain50-kitchen protocol with task memory [9].

| Method | Atomic-Seen | Composite-Seen | Composite-Unseen | Overall ↑ |
| :--- | ---: | ---: | ---: | ---: |
| **InteractVLA + RSI** | **93.6** | **89.2** | **84.8** | **89.4** |
| HexaShell 1.5† | 91.1 | 85.0 | 80.0 | 85.6 |
| Awomo-0.5I | 86.5 | 58.8 | 45.2 | 64.4 |
| minionx | 81.8 | 60.0 | 43.6 | 62.6 |
| Awomo-0.5V | 85.0 | 60.4 | 39.4 | 62.5 |
| Axiom-0 | 86.5 | 59.4 | 35.8 | 61.6 |
| HarnessVLA [36] | 87.8 | 43.8 | 42.5 | 59.2 |
| Paimon-0 | 79.3 | 55.8 | 36.7 | 58.1 |
| Xiaomi-Robotics-1 | 80.2 | 57.1 | 32.1 | 57.4 |
| Meituan-Robotics-0 | 77.3 | 57.8 | 27.1 | 55.0 |

**Evaluation notes.** Published VLA and agent baselines are from [25, 36]. † HexaShell 1.5 uses the pretrain50-kitchen protocol with task memory [9]. Added baselines use the official 50-task multi-task leaderboard (updated November 13, 2026). Split and Overall values are copied as published. Selected comparisons, not the complete leaderboard; ranks apply only to the methods shown. Sources accessed November 15, 2026.

Baseline source: [RoboCasa365 official leaderboard](https://robocasa.ai/leaderboard).

### Real world / Dexmal

Real-world manipulation on Dexmal. Task score and execution time (s) across four tasks.

| Task | VLA<br>Score ↑ | VLA<br>Time (s) ↓ | Agent + VLA<br>Score ↑ | Agent + VLA<br>Time (s) ↓ | Agent + InteractVLA<br>Score ↑ | Agent + InteractVLA<br>Time (s) ↓ |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| Tabletop Tidying | 31.2 | 97.4 | 65.5 | 372.0 | **92.5** | **295.8** |
| Upright Bottle | 16.9 | 63.8 | 37.9 | 226.8 | **74.6** | **217.1** |
| Water Pouring | 41.6 | 27.4 | 38.5 | 97.0 | **79.2** | **71.2** |
| Move Cube | 51.5 | 22.9 | 86.7 | 165.2 | **97.6** | **67.5** |

**Evaluation notes.** 50 demonstrations per task train an identical MR-0 backbone; each method is evaluated over 30 trials per task. Score is mean normalized task score, not binary success rate. Detailed scoring rubrics and standard deviations are in the supplementary material.

## Quick start

### 1. Install the core package

Use **Python 3.10 or newer**. Model inference additionally needs the environment and hardware required by your chosen upstream model.

```bash
git clone https://github.com/LingwuPro/InteractVLA.git
cd InteractVLA

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

### 2. Prepare a checkpoint and adapter

The release includes the interaction code and platform configurations. Supply these deployment-specific inputs separately:

- A compatible pretrained checkpoint and its complete upstream model implementation.
- An adapter manifest describing native action channels, normalization, geometry, gripper mapping, and control timing.
- Simulator assets or a robot controller and its calibration, when executing actions.

The manifest must agree with the checkpoint and selected platform configuration. See the [adapter specification](docs/implementation-guide.zh-CN.md#adapter-manifest) for required fields and coordinate conventions.

### 3. Start the tool server

<a id="backends"></a>
**StarVLA / PyTorch**

Install the full StarVLA model dependencies matching your checkpoint, then run:

```bash
source setup_env.sh libero

python -m interactvla.cli \
  --backend starvla \
  --checkpoint /path/to/checkpoint \
  --manifest /path/to/adapter.json \
  --config code/mr-starvla/examples/KeypointFlow/libero.json
```

**π0.5 / OpenPI / JAX**

Install a checkpoint-compatible OpenPI, JAX, and Flax environment. The optional extra supplies the package's JAX dependencies; it does not install OpenPI or model weights.

```bash
python -m pip install -e '.[pi05]'
source setup_env.sh libero

python -m interactvla.cli \
  --backend pi05 \
  --openpi-config YOUR_OPENPI_CONFIG \
  --checkpoint /path/to/checkpoint \
  --manifest /path/to/adapter.json \
  --config code/mr-starvla/examples/KeypointFlow/libero.json
```

The server listens on `127.0.0.1:18200` by default. Add `--controller-factory package.module:factory` to connect an executor. Without an executor, the service can generate and inspect proposals from supplied observations.

### Platform configurations

| Configuration | Generated chunk | Maximum executed prefix | Control rate |
| :--- | ---: | ---: | ---: |
| [LIBERO](code/mr-starvla/examples/KeypointFlow/libero.json) | 10 | 10 | 20 Hz |
| [RoboDojo](code/mr-starvla/examples/KeypointFlow/robodojo.json) | 50 | 16 | 20 Hz |
| [RoboCasa365](code/mr-starvla/examples/KeypointFlow/robocasa365.json) | 50 | 16 | 20 Hz |
| [Dexmal](code/mr-starvla/examples/KeypointFlow/dexmal.json) | 32 | 8 | 10 Hz |

These are configuration defaults. Running an environment also requires its dependencies, assets, and controller integration.

## Tool API

The runtime exposes a versioned JSON interface for an observe–propose–inspect–execute loop.

| Tool | Purpose |
| :--- | :--- |
| `observe` | Read a controller observation or provide one in inference-only mode. |
| `propose` / `guide` | Generate a native or geometrically guided action chunk. |
| `inspect` | Retrieve the full chunk, executed prefix, continuation, projections, and diagnostics. |
| `edit` | Create a new proposal with bounded local pose edits. |
| `backproject` | Recover a 3D point from a calibrated RGB-D view. |
| `execute` | Explicitly execute an accepted proposal prefix. |
| `stop` | Cancel subsequent commands and invalidate pending proposals. |
| `memory` | Store or retrieve versioned task records. |

Requests use `schema_version: "interactvla/1.0"`, a unique `request_id`, a `tool`, and `arguments`. State-dependent operations also carry `observation_id` and `state_version`. Repeating an identical request returns its cached result; reusing its ID with different arguments is rejected.

A controller implements `observe()`, `execute(command, expected_state_version=...)`, and `stop()`. Execution responses include status, an incremented state version, and measured state. See the [complete protocol](docs/implementation-guide.zh-CN.md#工具协议) and [runtime tests](tests/test_interactvla.py) for details.

## Code map

```text
InteractVLA/
├── code/
│   ├── interactvla/             # Public package and tool server
│   │   ├── sampling.py          # PyTorch guidance and soft-time objective
│   │   ├── geometry.py          # Native actions → command poses
│   │   ├── jax_backend.py       # JAX geometry and sampling
│   │   ├── models.py            # StarVLA and OpenPI adapters
│   │   ├── feedback.py          # Projections and motion edits
│   │   ├── runtime.py           # Tool protocol and execution lifecycle
│   │   └── cli.py               # Server entry point
│   ├── mr-starvla/              # Configs and evaluation integrations
│   └── work_dirs/              # Additional environment integrations
├── skills/SandboxRSI/           # Sandbox and recovery resources
├── tests/                      # Guidance, geometry, and runtime tests
├── docs/                       # Detailed implementation guide
├── pyproject.toml
└── setup_env.sh
```

Useful entry points: [LIBERO evaluation](code/mr-starvla/work_dirs/keypoint_flow/evaluation), [RoboCasa integration](code/work_dirs/robocasa365_agent), and [SandboxRSI](skills/SandboxRSI).

## Tests

```bash
python -m pip install -e '.[test]'
python -m pytest tests -q
```

The included tests cover temporal gradients, PyTorch/JAX consistency, action masks, gripper geometry, compatibility checks, execution versions, and local edits. They use synthetic fixtures; checkpoint inference, full simulator evaluations, and physical robot execution require their respective environments.

## Citation

```bibtex
@misc{zhao2026interactvla,
  title = {Turning VLAs into Interactive Tools for Agentic Robotic Manipulation},
  author = {Chen Zhao and Zipeng Ye and Shifeng Bao and Yihan Lin and
            Haitao Shen and Yining Luo and Yuchen Xie and Jing Zhang},
  year = {2026},
  url = {https://openreview.net/forum?id=ytXuX6Zm6J}
}
```

## Acknowledgments

InteractVLA builds on pretrained VLA models and robotics environments. Bundled third-party notices are preserved in their original locations, including the [StarVLA license](code/mr-starvla/LICENSE). Upstream model weights, implementations, and environment assets remain subject to their respective terms.

<div align="center">

**[Explore the project →](https://lingwupro.github.io/InteractVLA/)**

Source lives on `main` · The project website is maintained on [`gh-pages`](https://github.com/LingwuPro/InteractVLA/tree/gh-pages)

</div>
