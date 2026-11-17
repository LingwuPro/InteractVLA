部署入口（2026-10-09）

工作目录：mr-starvla；以下命令在仓库根目录执行。
日志、权重、仿真资产、测试结果统一保存在 work_dirs/astra_deploy。
参考 starvla_qwen35_v2 环境只读；新增 π0.5 环境为 ../conda_env/astra_pi05。

已验证：π0.5 LIBERO 单回合 70 步成功；LIBERO-plus 单回合 117 步成功。
StarVLA RoboTwin 单回合执行 400 步，任务未成功；通信、观测、动作和历史链路通过。
SimplerEnv 原版 WidowX、Google Robot 各通过 20 步渲染与控制；新版另通过 20 步。
两个 π0.5 权重共 44 个文件通过官方 MD5/CRC32C 校验。
完整路径、源码版本、checkpoint SHA256 和逐回合结果见 work_dirs/astra_deploy/deployment_report.json。

模型服务
  bash deployment/astra/service.sh status starvla
  bash deployment/astra/service.sh status pi05_libero
  bash deployment/astra/service.sh status pi05_base

service.sh 的 start/stop 子命令管理本任务服务，默认使用下述 GPU 和端口。
如需前台运行：
  GPU=0 PORT=18100 bash deployment/astra/serve.sh starvla
  GPU=1 PORT=18101 bash deployment/astra/serve.sh pi05_libero
  GPU=2 PORT=18102 bash deployment/astra/serve.sh pi05_base

端口已被服务占用时不要重复启动。首次推理包含 JAX/torch 编译时间。
StarVLA 使用 mr05_memory 的原生服务代码及 50k 完整 checkpoint；430k 是训练时的 VLM 初始化权重。
StarVLA 的历史按每个实际执行动作采样，使用原生 action_frame_v1 客户端；不能只按请求采样。
π0.5 使用固定 OpenPI commit 215abfb217dbac7d5f1273282331b9b1866c0479。
base 使用官方 Trossen ALOHA 适配器与归一化参数，调用时始终显式提供 prompt。
模型形状、编码和原生适配范围详见 models.json。

依次运行四类环境
  bash deployment/astra/run_sim.sh libero --port 18101 --steps 220
  bash deployment/astra/run_sim.sh libero_plus --port 18101 --steps 220
  bash deployment/astra/run_sim.sh robotwin --port 18100 --seed 100001
  bash deployment/astra/run_sim.sh simpler --steps 20

LIBERO 默认 spatial/task=0/episode=0。使用 --suite、--task、--episode 选择任务。
LIBERO-plus 使用独立源码、配置和初始状态，默认 task=0 为扰动变体。
不传 --port 是环境检查，只执行保持动作，不是模型评测。
--output 可指定单独结果目录，避免多次运行覆盖。
RoboTwin 默认 adjust_bottle；切换 --task 时同时传正确的 --instruction。
RoboTwin 连接模型时运行到任务步数上限或成功；--steps 仅控制不连接模型时的环境检查。
RoboTwin 此入口不做专家可行性筛选，结果标记 expert_filtered=false，不是官方 benchmark 总分。
StarVLA 服务维护当前 episode 历史，同一端口不要同时连接多个仿真 episode。

SimplerEnv 原版
  bash deployment/astra/run_sim.sh simpler --task widowx_put_eggplant_in_basket --steps 20
  bash deployment/astra/run_sim.sh simpler --task google_robot_pick_coke_can --steps 20

本机原版默认 GPU=3，并显式选择渲染设备 cuda:0。
GPU 0、1 的最小 SAPIEN 2 图像读取测试发生阻塞，GPU 3 通过；GPU 可用性需要按实际主机核验。
原版保持 SAPIEN 2.2.2 和 ManiSkill2_real2sim。
另提供独立的官方 ManiSkill 3 移植任务入口：
  bash deployment/astra/run_sim.sh simpler_ms3 --steps 20
它使用本地 Mesa lavapipe 软件 Vulkan，只有四个 WidowX 任务，结果不能混入原版评测。

复现与检查
  bash deployment/astra/configure.sh
  bash deployment/astra/bootstrap_pi05.sh

configure.sh 重建本机仿真配置并检查资产路径。
bootstrap_pi05.sh 在指定 conda prefix 缺失时创建环境，再按锁文件安装及验证权重。
锁文件中的 editable 路径指向本次部署的固定 OpenPI 源码，迁移机器需先准备相同源码。
模型下载使用用户提供的代理；推理和仿真入口会关闭代理。
所有可写缓存位于本任务目录，编译临时文件位于 /tmp/astra-work_dirs-UID。
GPU Vulkan/EGL 配置在任务目录中，不修改系统驱动。

研究协议边界
当前 checkpoint 是原生动作策略，没有训练完整 pose 关键点条件、条件草图源或概率 readout。
不能把双臂关节动作裁成单臂末端动作；不能把 pi05_base 的 Trossen 关节直接交给 AgileX。
跨本体组合需要明确 FK/IK 或重训练动作接口，以及对应数据和控制器验证。
这次没有调用 GPT-6 API，没有伪造概率或风险认证，也没有启动新的训练作业。
参考协议引用的 keypoint_flow_theory.md 在给定目录不存在；不能声称已核实其定理。
单回合 smoke test 仅验证部署链路，不代表基准成功率或零样本泛化能力。
