# 本地声音情绪分支

本分支使用公开 `iic/emotion2vec_plus_base` 预训练权重，输出九种声音情绪类别的模型支持分数。分数不是情绪强度，也不是经过本项目校准的概率。正式五档融合由 `affect_fusion.py` 负责。

## 安装和运行

在项目根目录使用现有面试环境：

```powershell
.\.venv-suit\Scripts\python.exe scripts/prepare_speech_emotion.py
```

安装器只下载公开模型和固定依赖，不读取面试音视频。独立依赖目录为 `.runtime/emotion2vec`，固定 `omegaconf==2.4.0`、`antlr4-python3-runtime==4.13.2`。基础 `.venv-suit` 需要既有的 PyTorch、NumPy、PyYAML、WebRTC VAD、requests 和媒体解码依赖；本机验证环境为 Python 3.12、PyTorch 2.14 CPU、NumPy 1.26.4。脚本不升级主环境。

权重、配置和词表存放在 `suit-demo/models/emotion2vec-plus-base`。安装时按固定 ModelScope revision 下载并验证官方 SHA256；运行时首次载入再次校验。权重原文件约 1.12GB，包含训练状态。运行使用 `torch.load(weights_only=True)`，逐键核对推理参数形状；未包含于微调权重、且不参与 `extract_features` 的重建 decoder 被显式移除，不以随机参数替代缺失权重。

模型结构固定自 FunASR 1.4.24 的六个 MIT 源文件，位于 `src/mmsa_v2/vendor/emotion2vec`，保留 LICENSE、上游/本地文件哈希和改动说明。主服务不导入 FunASR 的整套训练/ASR 自动发现代码，也不自动安装或联网。模型单例、两条 CPU 计算线程、串行锁限制同时推理。

## 接口和证据

`analyze_speech_emotion(path, window_seconds=5)` 对本地原音解码为 16kHz 单声道，按实际边界生成五秒窗口。语音模型采用官方归一化、编码特征平均、分类头及 softmax 推理步骤。

每窗记录九类 scores、首选类别、RMS、发声比例，以及 30ms VAD 检出的原片时间区间。静音、削波、发声不足的窗口保留缺失，不推断中性；缺模型、坏哈希和缺依赖抛出可重试错误。输出 provenance 包含模型哈希、原片哈希、窗口长度和 `network_used=false`。

## 已完成验证

- 静音不调用模型、没有音轨不填中性、VAD 原始边界、非有限输入拒绝：专属测试通过。
- 官方示例音频已本机推理成功。
- 郭梦琪 F6 原片 50.89 秒：11 个时间窗本机推理，冷载加推理约 12.84 秒；没有写入真实作答或评分。
- 同一原片首五秒与未修改的 FunASR 1.4.24 `Emotion2vec.inference` 对照，九类结果最大绝对差为 **0.0**。验证结果在 `suit-demo/local-data/v2/validation/speech-emotion-official-parity.json`。

该验证证明接入和官方模型推理一致，不等于已验证本面试场景的情绪识别准确率。
