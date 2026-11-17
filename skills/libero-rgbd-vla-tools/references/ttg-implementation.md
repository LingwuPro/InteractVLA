# 冻结 VLA 的 test-time guidance：详细实现

阅读定位：第 1 节先说明两个评测共用的 soft-time 主线及 RoboDojo 的实际配置；第 2 节单列额外的 `four_anchor` 结构消融；第 3 节说明 LIBERO-plus 的目标来源与 codec 接入；第 4 节是当前 RGB-D/Astra 工具；第 5 节是移植核对与源码入口。

## 1. 共同骨架与目标来源

这些实现冻结模型参数，每个 chunk 只采一次原始高斯、完成一次原生长度的去噪。目标 G 仅进入模型外部的可微几何损失，不修改原任务文本、attention、condition token、checkpoint 或初始噪声。没有 optimizer、训练、额外完整候选采样或置信度 readout。当前观测的多模态前缀可以缓存；换观测后重新编码。

`keypoint_flow_protocol.md` 还描述了带噪草图、额外 clean 条件 token 和概率 readout 的训练设计。它们不是这里的冻结模型 TTG，不能按理论接口给现有模型增加尚未训练的能力。

| 分支 | 几何目标来源 | 模型与原生动作 |
|---|---|---|
| RoboDojo Astra soft-time 主线 | 原始语言匹配的训练演示；用当前机器人状态和本次模型预测选择单个完整双臂目标 | B22 QwenMoTExpert；双臂 joint D14/state14，H50/N10，执行前 16 步 |
| LIBERO 历史 soft-time 主线 | 演示记录重建 command，或演示在目标变体中的 replay measured/command pose | 官方冻结 π0.5；JAX，H10/N10，7 维 delta OSC 动作及模型 padding |
| LIBERO 当前在线 soft-time 工具 | 当前 RGB-D、相机标定、机器人自身状态及已执行历史 | 同一冻结 π0.5；完整目标由会话明确提供，不从演示补点 |
| RoboDojo 附加消融 `four_anchor` | 复用训练集检索，改为同一演示的四个时刻目标 | 同一 B22/原生接口；损失改为四时刻均值乘 t²，同时改变 fm_time 与 cap |

历史离线分支可能读取 HDF5、replay 和机器人仿真状态；这不扩大在线 RGB-D 工具的权限。训练集检索也要显式声明为训练数据辅助策略，不能称为无演示输入的视觉规划。

### 1.1 时间方向与求导对象

统一记 canonical 时间 `t: 0 → 1`，当前 noisy actions 为 x，完整 clean 估计为 m：

```text
m = x + (1-t) * v_canonical
g = grad_x L(codec(m), G)
x_next = x + dt * (v_canonical - correction(g))
```

保留 expert 输出对 x 的 Jacobian；冻结参数不等于把整个 expert 放进 `no_grad`。每一步积分后断图，避免跨整个去噪轨迹反传。

π0.5 原生时间 `tau: 1 → 0`，`t=1-tau`，原生输出是 velocity：

```text
m = x - tau * v_native
g = grad_x L(codec(m), G)
x_next = x + delta_tau * (v_native + correction(g))
# delta_tau < 0
```

RoboDojo B22 的 action head 在当前代码中返回 clean actions，须按 `v=(clean-x)/(1-t)` 恢复原生速度。不要把 B22 的 clean 输出当作 π0.5 velocity，也不要混用两个更新符号。

### 1.2 两个平台共用的 soft-time 主线

RoboDojo 的 Astra 主线已经使用 soft-time；four_anchor 单独作为额外结构消融。复述评测方案时，分别写明主线和消融，避免用“RoboDojo=four_anchor、LIBERO=soft-time”概括。

共用的核心是：对一个完整目标 G，先计算每个预测执行时刻 h 的完整 pose 误差 E_h，再用 canonical 时间相关的 logsumexp 归约：

```text
L_soft(t) = -(1-t)² * (logsumexp(-t²/(1-t)² * E_h) - log(H))
L_guidance(t) = ((1-t)/t) * L_soft(t)  # fm_time=true; t=0 uses explicit zero
```

位置、朝向和夹爪先组成同一个 E_h，再共享 soft 权重。两个平台均通过 expert 对 noisy actions 求梯度，以正确时间符号更新，并在指定窗口外使用原生速度。平台适配改变目标来源、codec、动作维度和 RMS 有效维度；这些字段要单独记录。

| 实现项 | RoboDojo Astra 主线 | LIBERO 当前在线主线 |
|---|---|---|
| 损失归约 | `matching=soft_time`，在 H50 上匹配单个双臂目标 | `soft_time`，在 H10 上匹配单个单臂目标 |
| 额外时间因子 | `fm_time=true`，乘 `(1-t)/t` | `fm_time`，原生时间写为 `tau/t` |
| 相对修正预算 | `cap=0.3`，全 H50×D14 RMS | `cap03`，有效前 7 通道 RMS |
| 引导窗口 | `0.5<=t<0.8` | `0.5<=t<0.75`，即 `0.25<tau<=0.5` |
| N10 下实际窗口 | canonical 步骤 5、6、7 | 通常同为中间 3 步，检查浮点边界日志 |
| 目标选择 | 第 4 去噪步以模型预测选择训练参考 | 当前 RGB-D 完整目标；历史研究另有 clock/local/recorded 来源 |

#### RoboDojo 主线的单目标检索与配置

复用第 2.2 节的训练轨迹进度搜索和模型兼容性选择：原始语言精确匹配训练演示；每条演示在单调 33-state 窗口中对齐当前机器人状态；用去噪步骤 4 的 `clean[:,15:16]` 对候选 `action[p+15]` 进行完整双臂几何比较。

主线选中后保留的是**单个 `[B,14]` 目标**，不展开成四帧。`Geometry.errors` 将整个 `[B,50,14]` clean 序列与该目标比较，得到 `[B,50]` 误差供 soft-time 匹配。`max_target_error` 检查的是检索时的单点兼容性；未通过则本 chunk 不施加引导。

已评测主线的参数映射如下，仅列实现配置：

| label | scale | max_target_error | matching | fm_time | cap |
|---|---:|---:|---|---|---:|
| `astra_s30` | 30 | 1 | soft_time | true | 0.3 |
| `astra_s30_gate4` | 30 | 4 | soft_time | true | 0.3 |
| `astra_s30_gate6` | 30 | 6 | soft_time | true | 0.3 |
| `astra_s30_gate10` | 30 | 10 | soft_time | true | 0.3 |
| `astra_s15_gate10` | 15 | 10 | soft_time | true | 0.3 |
| `astra_s60_gate10` | 60 | 10 | soft_time | true | 0.3 |

这些配置共同使用 `source=model_retrieve`、`selection_step=4`、`fixed_step=15`、`lookahead=16`、`start=0.5`、`end=0.8`。`gate6` 表示兼容性误差门限 6，不表示只引导 6 步。源码另有只匹配前 16 步的 `soft_prefix_time` 分支，上述主线配置实际使用的是全 H50 的 `soft_time`。


## 2. 额外结构消融：RoboDojo four_anchor

### 2.1 名称与固定接口

`four_anchor` 指一个候选 chunk 内 **4 个固定执行时刻**的完整双臂目标，不是四条检索轨迹或四个任务领域。

它沿用主线的模型、训练库检索和外部几何求导骨架，但将 `soft_time` 改为 `prefix_t2`、`fm_time=true` 改为 false、`cap=0.3` 改为 0.1。因此它是额外方法变体，不能充当跨平台 soft-time 主线的替代说明，也不是仅调整 scale/gate 的纯参数对照。

冻结模型和训练库路径由运行配置提供。保持 BF16、原生归一化、三路 RGB、D14、H50、N10 和 execute16。14维按每臂6个关节加1个夹爪排列，不转换成 LIBERO 七维 OSC。

读取 `meta/episodes.jsonl`，按原语言指令精确筛选，再从 parquet 读取 `observation.state` 和 `action`。默认每条指令最多取前 100 条演示，不跨语言模糊匹配；没有精确匹配时返回无目标。该配置不执行视觉检索。

### 2.2 两阶段参考检索

每次请求使用原始机器人状态；适配器提供 `ttg_raw_state` 时优先使用，否则从模型 state 的 q01/q99 反归一化恢复。

第一阶段是每条训练轨迹内部的单调进度匹配：

1. 为每个 `(case, instruction)` 保存每条轨迹的进度 `p_j`；新 case 或 chunk0 重置为 0。
2. 在 `[p_j, min(p_j+32, T_j-1)]` 中寻找与当前 14 维原始状态最接近的训练状态。
3. 距离为逐维平方差加权和，夹爪通道 6、13 的权重为 4，其余为 1。
4. 更新该轨迹进度，不允许向后搜索。对齐阶段使用的是机器人 state 距离，不是物体位置或完整 pose 距离。
5. 为每条轨迹提出单点候选 `action[min(p_j+15, T_j-1)]`，即 `lookahead=16`。

第二阶段是跨轨迹的模型兼容性选择：

- 在同一次去噪的第 4 步（0-based，canonical `t=0.4`）读取 `clean.detach()`。
- 取预测 clean 的执行索引 15，与每条轨迹的单点候选通过同一个双臂几何 codec 比较。
- 选择完整 pose 误差最小的训练轨迹。误差大于 `max_target_error=1` 时，本 chunk 不施加几何修正；该值是无量纲组合平方误差，不是 1 米或成功概率。
- 选中轨迹后，取其 `p+[3,7,11,15]` 四个 action，越过演示末尾时钳制到最后一帧。
- 这些目标固定到本次 chunk 的 0-based 执行索引 `[3,7,11,15]`，也就是第 `[4,8,12,16]` 个命令。选择完成后不在后续去噪步重新检索。

注意：检索门限用的是单点候选兼容性，后续才计算四锚点损失；不能把它写成对四个锚点分别过门限。即使门限未通过，各训练轨迹的局部进度也已更新。

### 2.3 完整双臂 pose codec 与损失

`Geometry.decode` 先把用于损失的归一化预测限制到 `[-1,1]`，再用 checkpoint 的 action q01/q99 解码为物理 joint/gripper 命令。这个内部裁剪不是一次新的动作采样或最终动作覆盖。

`Geometry.fk` 从 `X5A.urdf` 的关节 origin、axis 构造可微旋转链。预测和训练目标经过同一套 FK，在每臂本地链坐标中得到位置 p、旋转矩阵 R 和夹爪命令宽度代理：

```text
w = 2 * (-0.01 + 0.054 * q_gripper)
```

它不是接触后实测指间距，也不能套用 LIBERO 的开闭宽度假设。每个锚点必须同时约束两臂的全部字段。

对锚点 k，冻结实现的误差为：

```text
E_k = sum_over_arms ||(p_k - p_goal_k) / 0.05||²
    + sum_over_arms ||R_k - R_goal_k||_F² / (2 * 0.5²)
    + sum_over_arms ((w_k - w_goal_k) / 0.04)²
L_four = t² * mean_k(E_k), k ∈ {3,7,11,15}
```

这里的 t² 仅是损失幅度因子；四个固定目标的误差取均值，不执行 soft-time 的 logsumexp。直接对四个锚点误差取 softmin 会偏向容易满足的锚点，改变“四个时刻分别约束”的语义，不应在保留 `four_anchor` 名称的同时悄悄替换。

旋转使用平方 chordal 距离，不是直接相减 quaternion。位置、旋转、夹爪尺度分别对应米、旋转尺度和米；组合误差无量纲。

虽然损失只读这四个执行时刻，梯度仍通过完整 action expert 对整个 noisy tensor x 求导；attention 的跨时刻依赖可能把修正传播到其他位置。没有把梯度硬 mask 到这四帧，也没有硬替换四条输出命令。

### 2.4 生成窗口、限幅与完整配置

引导只在 `0.5 <= t < 0.8` 开启。N10 下对应步骤 5、6、7，最后步骤 8、9 继续原生更新。第 4 步只完成参考选择，不反传引导梯度。

```text
g = grad_x L_four
raw = scale * g
factor = min(1, cap * RMS(v_native_canonical) / max(RMS(raw), 1e-12))
correction = factor * raw
x_next = detach(x + (1/N) * (v_native_canonical - correction))
```

这里的 RMS 在全部 H50×D14 上计算，`cap=0.1` 是相对速度修正预算，不是物理位移限制。`fm_time=false`，因此除了 `prefix_t2` 自带的 `t²` 外，不再乘 `(1-t)/t`。

冻结 `setting.json`：

```json
{
  "name": "model_retrieve_four_anchor_s30_finish",
  "scale": 30,
  "matching": "prefix_t2",
  "source": "model_retrieve",
  "start": 0.5,
  "end": 0.8,
  "cap": 0.1,
  "lookahead": 16,
  "fixed_step": 15,
  "position_scale": 0.05,
  "rotation_scale": 0.5,
  "width_scale": 0.04,
  "fm_time": false,
  "visual_topk": 5,
  "max_target_error": 1,
  "relative_motion": false,
  "selection_step": 4,
  "enabled_instructions": [],
  "minimum_chunk": 0
}
```

`visual_topk` 在该 source 下不参与选择；`enabled_instructions=[]` 表示不额外限制指令；`minimum_chunk=0` 表示不额外延后启用；`relative_motion=false` 表示目标是训练绝对 joint 命令，不把演示 joint 增量平移到当前状态。

### 2.5 实现调用顺序

```text
encode current RGB/language prefix with frozen model
build normalized robot state
seed = uint32_le(SHA256(case + ':' + ttg_seed + ':' + chunk)[:4])
draw one native Gaussian tensor [1,50,14]
target = None
for n = 0..9:
    t = n / 10
    active = target exists and 0.5 <= t < 0.8
    detach x; require its gradient only if active
    clean = frozen_head.denoise(prefix, x, state, t)
    if n == 4:
        target = choose_training_trajectory_and_four_anchors(clean.detach())
    v = (clean - x) / (1-t)
    if active:
        v -= RMS_capped(scale * grad_x(t² * mean(full_pose_errors)))
    x = detach(x + v/10)
return normalized actions [1,50,14]
execute first16 using unchanged RoboDojo adapter; observe again
```

模型参数保持 `requires_grad=False`；引导步须使用 `torch.inference_mode(False)` 并保留 denoiser 对 x 的求导路径。目标检索不反传，不对训练库或模型参数求梯度。

## 3. 共用 soft-time 在 LIBERO-plus 的目标与 codec 接入

### 3.1 Command pose 与 measured pose 分开

LIBERO 原生 7 维输出是 delta OSC 命令，不能直接把前 3 维当世界位置，也不能把预测相对旋转当绝对目标旋转。

历史实现区分两种 pose：

- measured pose：机器人真实末端位姿和实测夹爪状态。
- command pose：控制器收到命令后要求的末端位姿，以及理想夹爪命令开口。它受控制器缓存、动作裁剪、坐标变换和夹爪积分约定影响。

目标必须声明是哪一种。当前 command codec 中，正夹爪 action 表示闭合，负值表示张开；命令开口由累积命令状态推导，不用被夹物体的实测宽度替代闭合目标。

### 3.2 外部目标来源：recorded、clock、local

**`recorded_clock_command`**：从原始演示的 pre-action state/action 重建命令序列。`recorded_reference_audit/recover_commands.py::reconstruct` 在每条记录 state 上恢复 MuJoCo 机器人状态，调用原生 OSC 的 `update(force=True)`、`set_goal(action[:6])`，再按每个控制周期的 physics ticks 更新夹爪命令。记录 controller goal position/orientation 和 `(gripper.current_action+1)*0.04` 开口；没有通过物理 rollout 得到新的测量轨迹。

这些 command 保存为机器人 base 坐标的 `[position3, quaternion_xyzw4, width1]`。`recorded_command_reference` 在前面补初始 pose，使索引 h≥1 对应 action h−1。每次规划以 `min(executed_controls+10, final_index)` 取完整目标，并在本 chunk 内保持不变。

重建保留明确假设：演示未记录控制器全部缓存；使用 fresh FK anchor；零旋转沿用先前重建 goal；夹爪从原生初始值开始；记录缺失的前几条动作不能补造；原场景目标没有按目标变体做几何适配。现有构造的 index0 仍来自初始参考 pose，因此不能笼统称为整条入口完全不依赖 replay。

**`clock_command`**：目标变体中按演示动作 replay，读取原生控制器实际 goal 与夹爪命令。索引同样为已执行控制步数+10。它与 recorded 方案改变的是目标来源，不是去噪内部的时间匹配。

**`local_command`**：在目标变体 replay 的 measured 完整 pose 序列中，于 `[previous_progress, previous_progress+20]` 内寻找与当前机器人观测最近的进度，然后取该进度+10的 command pose。进度不回退，每个 H10 chunk 最多向前搜索 20 个参考控制步。初始进度为 0，初始目标为 command10。

**`recorded_local_command`**：进度仍由目标变体 replay measured pose 对齐，仅把目标命令换成 recorded command；不能称为完全无 replay 的方法。

这些历史来源与当前 RGB-D 工具的目标生成互斥。在线使用同一损失/采样器时，应将 G 换成当前观测得到的完整 pose，不沿用历史演示选点器。

### 3.3 可微 command codec

先按原模型 action quantiles 反归一化，再按 LIBERO OSC 原生 `input_min/max` 裁剪并映射为位置/旋转增量。

`track_pose025` 对未来前序命令采用 25% 跟踪近似。对执行索引 h：

```text
p_command[h] = p_current + 0.25 * sum_{j<h}(delta_p[j]) + delta_p[h]
R_achieved starts at R_current
R_command[h] = Exp(delta_r[h]) @ R_achieved
R_achieved = Exp(0.25 * delta_r[h]) @ R_achieved
```

当前时刻使用完整控制增量，前序增量才使用 25% 近似。它不是 FK、接触模拟或真实闭环动力学；实际控制仍交给原生 OSC。

夹爪原生 `sign(action)` 几乎处处不可导，故 `gripper_expectation` 使用以下代理：

1. 在 `[-1,1]`、间隔 0.25 的 9 个命令状态上初始化当前夹爪状态的 one-hot 分布。
2. 每个预测动作计算闭合概率 `Phi(action_gripper * t / tau)`，其中 Phi 是标准正态 CDF；另一分支为张开。
3. 用有饱和边界的开/闭状态转移传播分布，计算对目标开口的**平方误差期望**。
4. `tau=0` 时按实际正/负/零 action 的闭合/张开/保持极限处理，避免除零。

这不同于只对期望开口取平方误差，也不是把执行夹爪替换成概率动作。最终继续执行原生夹爪 action。`command_width` 中的 sign 积分用于命令语义/诊断；可微损失使用上述期望项。

### 3.4 完整 pose 误差、soft matching 与 fm_time

每个预测执行时刻的误差包含同一个时刻的位置、旋转、夹爪：

```text
E_h = 0.5 * sum((delta_p / control_position_scale)²)
    + 0.25 * ||R_h - R_goal||_F² / mean(control_rotation_scale)²
    + 0.5 * expected_squared_width_error / gripper_width_scale²
```

尺度取自当前控制器；常用量级为位置 0.05 m、旋转 0.5、夹爪 0.04 m。不得把这些数字当跨机器人通用权重，也不要照搬 RoboDojo 的双臂求和系数。

`soft_time` 在全部 H10 个误差上进行时间相关归约：

```text
t = 1 - tau
logits_h = -(t² / tau²) * E_h
L_soft = -tau² * (logsumexp(logits) - log(H))
weights_h = softmax(logits_h)
grad_x L_soft = t² * sum_h(weights_h * grad_x E_h)
```

由此软匹配与 `t²` 梯度幅度同时变化。不能用普通 softmin 替换后仍称为同一实现。位置、朝向、夹爪先组成 E_h，再共享匹配权重；不能各自选择不同的最优时刻。

`fm_time` 额外将 loss 乘 `tau/t`；`t=0` 时明确取 0。这是额外日程，不是 soft-time 自带的 `t²`。采样使用非奇异网格；终点诊断若需 `tau=0`，用明确 hard 极限，不计算 0/0。

对应的 `hard_t2`、`fixed_t2`、`mean_t2` 分别是最小误差、最后一帧误差、全 H 均值再乘 t²，用于保持幅度因素可比较。hard 选择索引时 stop-gradient，再 gather 原误差求导。

### 3.5 中后段引导、原生收尾与 RMS 限幅

当前在线工具复用的 mode 为：

```text
soft_time_control_track_pose025_fm_time_gripper_expectation_late_half_native_finish_cap03
```

`scale` 由请求显式提供；例如 `scale=30`。这个 mode 的执行方式是：

- `late_half`：原生 `tau>0.5` 时不反传几何梯度。
- `native_finish`：`tau<=0.25` 时也不反传，让最后若干去噪步继续原生速度场。
- 实际引导窗口为 `0.25<tau<=0.5`，等价于 `0.5<=t<0.75`。N10 时通常是中间 3 步，按日志中的真实浮点时间核对边界。
- `cap03`：`effective_scale=min(requested_scale, 0.3*RMS(v_native)/(RMS(g)+1e-8))`，随后用正确的负 `delta_tau` 积分。

RMS 只在原生有效的前 7 个 action 通道上计算，padding 梯度设为 0，padding 原生采样保持不变。它与 RoboDojo 的全 H50×D14 RMS 不同。

`recorded_clock_command~soft_time_control_track_pose025_fm_time_gripper_expectation_s30` 是另一项完整配置：源为 recorded-clock、scale30，名称里没有 late-half/native-finish/cap03，不能擅自补上这些开关。目标来源、codec、损失归约、生成窗口和限幅分别记录，不能把它们合成一个含糊的“guidance30”。

JAX 中使用 `jax.vjp(..., has_aux=True)` 同时得到 loss 与共享 velocity/诊断，只在窗口内执行 input pullback；每步用 `jax.lax.stop_gradient(x)` 断图，不需要第二次完整去噪。

## 4. 当前 RGB-D/Astra 工具如何接入

入口 `online_tool/tool.py` 的 `BEST` 常量绑定上节的中后段/native-finish/cap03 mode。会话从当前两路 RGB、深度和标定确定完整目标：位置、`quaternion_xyzw`、`width_m` 都必须给出；双臂迁移时覆盖所有受控效应器。

```json
{"type":"vla","chunks":1,"scale":30,"goal":{"position":[0.0,0.0,0.95],"quaternion_xyzw":[1,0,0,0],"width_m":0.08}}
```

以上数字仅展示 schema，不能当作抓取模板。每生成并执行原生 10 步后重新观察；语言保持原始任务文本。额外抬高、退让、解卡或观察动作由会话通过 `servo/controls` 执行，与正常任务关键点引导分开记录。

RGB-D 点是可见表面点，不自动等于物体中心或可执行抓取 pose。对保存的 row/column、光轴深度、相机到世界变换和工具已处理的图像翻转保持同一约定；不要重复翻转。重投影只能使用当前已观测表面和按相机安装场景选择的标定。

工具包装层另做起始目标距离与完整候选残差检查；沿用 `SKILL.md` 的接口与拒绝逻辑。这些检查不属于最小采样器，也不输出经过校准的成功概率。发生直接接管、拒绝或重试时分别记实际调用，不把它们视为额外的梯度步骤。

## 5. 移植核对与实现入口

移植先固定 checkpoint、归一化、控制器、H/N、每次提交步数、相机顺序、双臂/夹爪维度和时间方向。关闭引导后，同观测同高斯应复现原生动作；再检查 expert→x 梯度、完整 pose 共享时刻、数值有限性及参数不变。任何 action 后处理都应包含在 codec/残差核对里，不在评分后悄悄改动作。

按请求记录：case、实际 layout/reset 身份、chunk、噪声种子、完整 setting、训练 episode/目标帧或观测选点依据、实际激活的去噪步、loss、梯度/原生速度/修正 RMS、最终动作及耗时。机器人命令 pose 的误差不等于真实接触状态，也不应转换成未经校准的置信度。

发布包中的 LIBERO 实现位于 `code/mr-starvla/work_dirs/keypoint_flow/evaluation/pi05_dynamic_guidance`；RoboDojo 几何参考位于 `skills/SandboxRSI/sources/dependencies/guidance.py`。完整模型、训练库和仿真资产由运行者提供。

| 实现 | 文件与符号 |
| --- | --- |
| RoboDojo 目标选择、FK、损失与采样 | `guidance.py`：`TrainingBank.goal`、`Geometry`、`reduce_errors`、`sample`、`GuidedPolicy.predict_action` |
| LIBERO 几何、追踪与夹爪代理 | `geometry.py`：`decode_tracking`、`expected_gripper_error`、`match_pose`、`reduce_errors` |
| LIBERO 生成窗口与 VJP | `sampler.py`：`DynamicSampler.sample_dynamic`、`correction_scale` |
| 几何配置解析 | `server.py`：`prepare_geometry`、`NATIVE_FINISH_CAPS` |
| 在线目标与工具请求 | `online_tool/tool.py`：`BEST`、`Session` |

训练集或离线演示参考不得自动成为在线观测工具的默认输入。
