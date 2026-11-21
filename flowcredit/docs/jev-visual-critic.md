# Jev 视觉 critic 实验（advisory only）

> 状态：实验，不接生产链路。Jev 的输出不改变 `project.json.quality`、运行终态或
> 任何 host verifier 的结论。

## 为什么做

`motion_frames.js` 能测 DOM 几何，但测不了：

- 两条腿看起来是否真的在蹬；
- 腿是不是穿过车架/轮子；
- 身体、腿、踏板的遮挡关系；
- 构图是否可读。

Jev 的 `noul` / `choice` / `score` / `box` + 图像输入可以补这一层。图像和 Box
目前是 TypeSafe 的 "Dreamer-ready" 实验特性，所以这里只做 shadow，不当裁判。

## 用法

凭据从 `~/.config/claimledger/jev.env` 读取（`TYPESAFE_API_KEY` /
`TYPESAFE_MODEL`），不会打印 key。

场景由 `config/scenes/*.json` 描述，核心代码不写死任何角色/交通工具：

```json
{
  "id": "pelican-bicycle",
  "subject": {"label": "cartoon pelican", "description": "white bird with a throat pouch"},
  "vehicle": {"label": "bicycle", "description": "two spoked wheels", "support_label": "pedal"},
  "features": {"two_legs": true, "legs_attach": true, "feet_on_support": true},
  "composition": "pelican riding a bicycle",
  "crop": [140, 90, 900, 530],
  "scale": 2
}
```

```bash
# 鹈鹕 canary（整帧 + 场景裁剪）
python3 scripts/jev_visual_critic.py \
  --run viewer/runs/run-20261114-131725-aee9 \
  --scene config/scenes/pelican_bicycle.json \
  --frame-ms 450 \
  --out /tmp/jev-full.json

# 紧裁剪（腿更大；用于判断腿/脚/踏板）
python3 scripts/jev_visual_critic.py \
  --run viewer/runs/run-20261114-131725-aee9 \
  --scene config/scenes/pelican_bicycle.json \
  --frame-ms 450 --crop 260 170 700 530 --scale 3 \
  --out /tmp/jev-legs.json

# 换场景只换 descriptor
python3 scripts/jev_visual_critic.py \
  --run viewer/runs/run-20261114-131725-aee9 \
  --scene config/scenes/robot_scooter.json --dry-run
```

`--dry-run` 只显示请求形状，不读 key、不发网络请求。

`src/flowagent/decisions/jev_visual.py` 里没有 `pelican`/`bicycle` 字样；测试会检查这一点。

## 实测

模型：`jev-1.14.0`。每次请求约 22,000–25,000 input tokens，成本约 **0.9–1.1k µUSD**。
图像需要 `TypeSafe-Danger-Zone-Images: true`。

早期实验用的是旧答案 key（`near_foot_on_pedal` / `leg_through_frame`）；场景化后统一为
`near_foot_on_support` / `legs_through_vehicle`，旧 key 在分类器里保留兼容。

| 输入 | two_legs | far_foot | occlusion | 结果 |
|---|---:|---:|---|---|
| 伪腿（整帧） | 0.60 | 0.63 | through_frame @ 0.54 | warn |
| 伪腿（紧裁剪） | 0.60 | 0.50 | through_frame @ **0.73** | **fail** |
| v5 好 rig（整帧） | 0.84 | 0.55 | through_frame @ 0.60 | warn |
| v7 好 rig（整帧） | 0.81 | 0.64 | through_frame @ 0.54 | warn |
| v7 好 rig（紧裁剪） | 0.87 | 0.80 | through_frame @ 0.46 | warn |
| v7 场景化重跑（整帧） | 0.84 | 0.53 | through_vehicle @ 0.37 | warn |

场景化后的真实请求（`scene=pelican-bicycle`）仍得到 `subject_visible=0.91`、
`vehicle_visible=0.98`、`legs_attach_to_body=0.94`；说明 descriptor 驱动没有破坏
原问题，同时 `robot_scooter` / `abstract_bounce` 的 dry-run 不需要改核心代码。

观察：

1. **紧裁剪比整帧更有区分度**：伪腿 `occlusion` 置信度升到 0.73，好 rig 只有 0.46。
2. `two_legs_visible` 对伪腿是 0.60，对好 rig 是 0.81–0.87；有信号，但不能单独当阈值。
3. Jev 对所有样本都倾向回答 `leg_through_frame`，低置信时应视为“可能遮挡”，不能硬 fail。
4. `box` 能给出髋/脚/踏板的近似位置（例如近脚与踏板 box 基本重叠），但还需要做坐标映射和人工校准。
5. 视觉 critic 的成本很低（约 1k µUSD/帧），但图像 token 不便宜；整帧 + 紧裁剪两次调用更稳。

## 场景化后的 held-out 检查

用手写的机器人/滑板车 fixture（`.deck` 支撑、两条腿从 `.hip` 出发）验证：

- 确定性质量门：`status=pass, score=100, coverage 8/8`；支撑面矩形距离 0，
  说明 `.deck` 已按 support 处理，不是把 deck 伪装成 pedal。
- Jev `scene=robot-scooter`：`two_legs_visible=0.89`、`legs_attach=0.82`、
  两个 foot-on-support 都是 0.78；但 `subject_visible=0.33`、
  `vehicle_visible=0.13`，advisory 判 fail。

这个 false negative 很重要：Jev 能看见“腿/支撑关系”，但不能稳定识别
“机器人/滑板车”这种新造型。它不能作为视觉终态裁判；必须和人工标签、
确定性几何、以及更细的裁剪/多视图组合一起校准。

## 当前结论

- Jev 图像输入可用，**适合做 advisory 的第二意见**；
- 单次整帧判断会过度报警，不能直接替代几何门；
- 紧裁剪能放大腿/脚信号，适合和整帧组合；
- 在接入运行时之前，需要：
  1. 建立人工标注集（好 rig / 伪腿 / 遮挡 / 审美）；
  2. 标定 `occlusion`、`two_legs`、`far_foot` 的阈值；
  3. 组合“整帧 + 紧裁剪”的多视图 verdict；
  4. 只进入 shadow，先不影响终态。

## 治理

- `config/online-policy.json` 的主 scope 仍是 audit-domain only；本次操作员要求的开放探索单独记录在 `experiments.jev_visual_shadow`（advisory、最多 10 次、无终态影响）。正式接 runtime 前仍需单独扩权。
- 图像会发送到 TypeSafe 外部服务；不要发送含密钥、用户隐私或未授权内容的图。
- `classify_visual` 的 `status` 只是 advisory；host 的确定性检查仍是最终事实。
- 凭据只从 env/env file 读取，不写入仓库、日志或 prompt。
