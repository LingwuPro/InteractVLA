---
name: libero-rgbd-vla-tools
description: 在 LIBERO-plus 中以 RGB-D、相机标定和机器人自身状态调用冻结 pi0.5，进行闭环规划、完整 pose 关键点引导及失败恢复；在线不读取演示或物体真值。
---

# LIBERO-plus 观测闭环工具

入口为 `code/mr-starvla/work_dirs/keypoint_flow/evaluation/pi05_dynamic_guidance/online_tool/tool.py`；采样服务为相邻 `server.py`。环境、模型路径与服务地址由发布包配置和命令行提供。

## 方法与来源

阅读[TTG 实现](references/ttg-implementation.md)、[观测恢复](references/observation-recovery.md)，按需查[公开任务技能](references/worldscape-skills-full.md)。主线采用完整 pose 的 `soft_time` 匹配；RoboDojo 的 `four_anchor` 是额外结构消融，不能当作同一匹配骨架。训练型条件 token/readout 设计也不代表冻结采样工具已具备对应训练能力。

在线目标来自当前观测；离线演示目标和训练集检索是不同信息合同，不能混合宣称无特权结果。公开技能中的工具名称须映射到实际接口，不自动取得额外信息权限。

## 观测与动作合同

规划读取 RGB、深度、相机内外参、机器人末端/关节/夹爪状态、原始指令及自身动作历史。不读取物体真值、隐藏目标、分割真值、演示或参考轨迹。官方初态仅用于 reset；原生成功信号仅用于评价，不反推隐藏目标。

图像采用 OpenCV 左上角原点与 `row,column`；深度是光轴 z，单位米。`world_from_camera` 为相机到世界变换，适配器处理过的图像翻转不可再次应用。可见表面点不是物体中心，也不是直接可执行抓取位姿。

完整目标包含世界坐标 `position`、`quaternion_xyzw`、命令开口 `width_m`。原生夹爪命令正数闭合、负数张开；命令开口来自控制 codec 的积分，不是接触后的实测开口。持续闭合目标为0，完全张开目标为0.08米。

## 工具流程

按递增编号原子提交 `command-*.json`，读取同编号响应与最新观测。工具支持 `observe`、`vla`、`servo`、`reset`、`stop`；请求字段以源码为准。每次 VLA 执行原生 chunk 后刷新观测，共享原控制预算。

先按冻结协议运行原生 VLA 基线，失败后依据当前观测介入。VLA 只接收原始任务文本；正常抓放目标可由完整 pose 引导，抬高避让、解卡、换视角等恢复动作通过直接控制执行。每段后重读观测，分别核验物体身份、抓持随动、支撑和释放。

采样器保留 soft-time、late-half/native-finish 和 RMS cap。`goal_too_far` 或 `guidance_unresolved` 表示未执行候选；返回零步不是新传感器观测。拒绝门限不是物理可达性保证或成功概率；候选误差通过也不保证实际跟踪。

RGB-D 重投影只使用当前可见点与正确场景相机标定，z-buffer 合并并记录孔洞；不得通过仿真重渲染补入未观测表面。手眼图像和固定视角各自核对标定。机器人/相机变化后重新测量目标，不沿用旧像素或世界坐标。

## 评价与恢复

固定任务、初态、随机噪声、checkpoint、配置及每轮预算，对照同一次纯 VLA 与恢复策略。分别报告单次成功、有限重试成功和直接恢复成本；不能将筛选成功视频或开发失败集当作全量效果。

成功仅以原生终态判断，局部到位不能代替；终态停止。服务重启不等于物理状态恢复，应重建初态并严格重放已执行动作、保持原观测顺序。记录可审查的观测与决策摘要，不保存内部推理过程；未获得实际 token usage 时记录 unavailable。
