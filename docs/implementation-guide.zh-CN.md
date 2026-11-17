# InteractVLA

冻结 VLA 的几何引导与交互式机器人控制接口。模型保持原生动作空间，目标只进入可微几何目标函数。推理直接加载原始 checkpoint。

## 运行

```bash
python -m pip install -e '.[test]'
source setup_env.sh libero
python -m interactvla.cli --backend starvla --checkpoint /path/to/checkpoint \
  --manifest /path/to/adapter.json \
  --config code/mr-starvla/examples/KeypointFlow/libero.json
```

π0.5 使用 `--backend pi05 --openpi-config CONFIG_NAME`，并安装与 checkpoint 匹配的 OpenPI、JAX 和 Flax 环境。StarVLA 使用与 checkpoint 匹配的完整模型仓库。本目录不包含模型权重、上游模型实现、仿真资产或机器人的标定文件。

`--controller-factory package.module:factory` 连接执行器，工厂接收 adapter manifest。未连接执行器时可以生成和检查提案。执行器实现 `observe()`、`execute(command, expected_state_version=...)`、`stop()`。每次执行必须返回 `status`、递增的 `state_version` 和实测状态；执行前需要在控制器端再次核对期望版本。

## 推理与几何

`code/interactvla` 是公共运行包。`serve_starvla` 和 `serve_pi05` 指向同一工具服务。PyTorch 和 JAX 使用相同的采样规则：10 次 Euler 评估，0.5、0.6、0.7 三次输入反向传播，0.6、0.7 两次更新前兼容性检查。视觉语言上下文按观测缓存，每个积分状态分离计算图。

完整位姿误差包含位置、旋转和夹爪宽度。位置尺度为 0.05 m，旋转尺度为 `sqrt(8) * sin(15°)`，夹爪尺度为标定宽度范围。一个动作时刻同时聚合所有激活分量及所有目标机械臂，然后计算软时间权重。

梯度增益为 1，校正 RMS 上限为目标有效通道原生速度 RMS 的 0.3 倍。兼容性使用未截断梯度、最小完整位姿误差及相对校正需求；阈值为 `exp(-1)`，等于阈值时接受。兼容性不是任务成功概率。

| 配置 | 生成长度 | 执行上限 | 控制频率 |
| --- | ---: | ---: | ---: |
| libero.json | 10 | 10 | 20 Hz |
| robodojo.json | 50 | 16 | 20 Hz |
| robocasa365.json | 50 | 16 | 20 Hz |
| dexmal.json | 32 | 8 | 10 Hz |

## Adapter manifest

manifest 必须声明 `action_dim`、`channel_names`、`valid_channels`、`normalization`、`horizon`、`execute_prefix`、`control_rate_hz`、`frame_id`、`flow_convention` 和 `effectors`。动作宽度包含模型 padding；padding 的有效掩码为 false。底盘等保留原生更新的通道写入 `unsteered_channels`，保持有效但不参与机械臂引导。

每个通道的归一化规则为 `identity`、`quantile`（`lower` / `upper`）或 `mean_std`（`mean` / `std`）。统计量来自所加载 checkpoint。`flow_convention` 为 `clean_action`、`increasing_velocity` 或 `decreasing_velocity`。

每个 effector 声明 `id`、`mode`、`motion_channels`、`base_transform`、`input_min/max`、`output_min/max` 和 `gripper`。`delta` 使用六维平移与旋转向量，声明 `delta_frame`（world/body），`tracking` 默认 0.25；`absolute` 声明 `rotation_format`（rotvec/xyzw）；`joint` 声明按动作顺序排列的 `joints`、`joint_min/max`，每个 joint 包含 `type`、`axis` 和齐次 `origin`。可选 `tool_transform` 表示末端固定变换。

gripper 声明动作 `channel`、`mode`、`width_min/max`。连续夹爪通过 `scale` 和 `offset` 映射到米；二值夹爪通过 `closing_polarity` 表示闭合方向，可声明 `command_scale/offset` 和 `open_command`、`closed_command`、`hold_command`。二值几何使用九状态分布和实测初始宽度插值。可选 `max_command_rate` 对有效动作通道设置每秒变化上限。

manifest 的动作顺序、归一化、坐标变换、控制器范围和夹爪极性必须对应部署的 checkpoint 和控制器。加载时检查结构、原生动作宽度和生成长度；实际控制器标定由部署方提供。

## 工具协议

所有请求包含 `schema_version: "interactvla/1.0"`、唯一 `request_id`、`tool` 和 `arguments`。生成、编辑、反投影、执行还包含 `observation_id` 和整数 `state_version`。重复请求返回缓存结果；同一 ID 不接受不同参数。

- `observe`：从连接的控制器读取观测，或在纯推理模式通过 `arguments.observation` 提交观测。
- `propose`：使用 `seed` 生成原生动作提案。
- `guide`：使用 `seed`、`target` 生成引导提案。
- `inspect`：按 `proposal_id` 返回完整动作、命令位姿、执行前缀、剩余索引、历史、投影和兼容性诊断。
- `execute`：按 `proposal_id` 和 `prefix_length` 显式执行已接受的提案。拒绝、旧版本及已消费提案均不可执行。
- `edit`：按 `proposal_id`、`first_index`、`last_index`、`translation`、`rotation_vector`、`effectors` 创建新提案。索引从零开始，末索引包含在内；旧提案的剩余部分需 `rebase: true`，此时索引相对剩余部分。
- `backproject`：使用 `view_id`、原始图像 `pixel` 和可选 `region`，在 5×5 邻域取有效深度中位数，至少需要五个有效像素。
- `stop`：取消后续命令并使待执行提案失效。
- `memory`：按 `mode`、`task_key`、`record` 写入不可变版本，按 `version` 读取。

观测包含 `id`、`state_version`、`frame_id`、`timestamp_s`、`state`、`instruction`。`state` 以 effector ID 为键，每项包含 `position`、`rotation`、`width`。StarVLA 另外接收 `images` 和经过原生输入变换的 `native_state`；OpenPI 接收 `policy_input` 并使用原策略输入变换。可选 `sensor_timestamps_s` 用于同步检查。

目标包含 `frame_id`、`observation_id` 和 `effectors`。每个目标 effector 包含 `position`、`rotation` 或 `quaternion_xyzw`、`gripper`（0 闭合，1 打开）、可选布尔 `components: [position, rotation, gripper]`。省略的机械臂保持原生更新。目标不指定固定到达步。

可选 `cameras` 以视角 ID 为键，包含 `intrinsics`、`camera_from_base`、`image_size: [height,width]`、`calibration_id`、可选对齐深度 `depth` 和 `display_transform`。数值投影保留光学深度、画内及遮挡标记。

局部位姿编辑最多平移 2 cm、旋转 10°，使用两端值和导数为零的连续偏移。关节命令通过阻尼最小二乘 IK 恢复，失败时不生成可执行动作。连续三次拒绝要求重新观测和任务重规划，六次拒绝结束尝试；接受提案清零拒绝计数。

## 目录与验证

- `code/interactvla`：公共采样、几何、模型适配、反馈与工具服务。
- `tests`：数学梯度、双后端一致性、掩码、夹爪、执行版本及编辑测试。
- `code/mr-starvla/work_dirs/keypoint_flow/evaluation`：LIBERO 原生与引导评估入口。
- `code/work_dirs/robocasa365_agent`：RoboCasa 控制与引导接入。
- `skills/SandboxRSI`：沙箱控制与恢复资源。
- `code/mr-starvla/starVLA/keypoint_flow`：服务兼容入口与原生评估辅助函数。

```bash
python -m pytest tests -q
```
