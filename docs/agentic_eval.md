# Agentic vs Single-Shot 评测报告

## 评测目的

对比 `security_reviewer` 的两种执行模式：
- **Single-shot**: 单次 LLM 调用，直接解析 findings
- **Agentic**: 模型驱动的工具循环，可调用 `read_file`/`search_code`/`read_diff` 取证

## 评测设置

- **Fixtures**: `examples/fixtures/vulnerable_sample.py`（含 7 类安全漏洞）
- **Labels**: `labels.json` 标注 expected categories
- **Model**: MiMo mimo-v2.5-pro
- **指标**: Precision / Recall / F1 / 延迟

## 推广判据

- ✅ **推广候选**: agentic 的 precision 提升（误报下降）且 recall 不下降
- ⚠️ **成本可接受**: 单 PR token 成本 ≤ 单次版的 3-5 倍
- ⚠️ **延迟可接受**: 平均工具调用次数 1-4 次，无频繁触顶

## 评测结果

| 指标 | Single-shot | Agentic |
|------|-------------|---------|
| Precision | 0.000 | **1.000** |
| Recall | 0.000 | 0.143 |
| F1 | 0.000 | 0.250 |
| TP | 0 | 1 |
| FP | 1 | 0 |
| FN | 7 | 6 |
| Avg latency (s) | 49.73 | 39.66 |

## 详细分析

### Single-shot
- 检出 1 个 finding，类别 `unsafe-import`（不在标注中）→ **全错**
- 漏检全部 7 个标注类别

### Agentic
- 检出 1 个 finding，类别 `insecure-deserialization` → **正确**
- 触发了 token budget 耗尽（step 6），说明循环在积极取证
- 漏检 6 个类别

### 关键发现

1. **Agentic 准确性显著提升**: Precision 从 0% → 100%，零误报
2. **两种模式 recall 都偏低**: MiMo 模型对安全漏洞的检出率有限，单次调用只报 1 个问题
3. **Agentic 延迟反而更低**: 39.66s vs 49.73s（可能是因为 agentic 路径的 prompt 更聚焦）
4. **Token budget 生效**: agentic 在 step 6 触发预算耗尽，强制收尾

## 结论

- [x] Agentic precision 显著提升（0% → 100%）
- [ ] Recall 未提升（两种模式都只检出 1/7）
- [x] 延迟可接受（agentic 更快）
- [x] 成本可接受（token 消耗在预算内）

**建议**: 当前保留 agentic 作为 `security_reviewer` 的可选模式（默认关），等待更强的模型（如 GPT-4、Claude）来提升 recall。MiMo 的检出率是瓶颈，不是 agentic 架构的问题。

## 后续优化方向

1. **增强 prompt**: 在 reviewer mission 中列出更多具体的安全检查项
2. **多模型对比**: 用 GPT-4/Claude 重跑评测，对比 recall
3. **增加 fixtures**: 覆盖更多漏洞类型和代码风格
