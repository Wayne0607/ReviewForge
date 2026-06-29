# Agentic vs Single-Shot 评测报告

## 评测目的

对比 `security_reviewer` 的两种执行模式：
- **Single-shot**: 单次 LLM 调用，直接解析 findings
- **Agentic**: 模型驱动的工具循环，可调用 `read_file`/`search_code`/`read_diff` 取证

## 评测设置

- **Fixtures**: `examples/fixtures/` 中的标注漏洞文件
- **Labels**: `labels.json` 标注每个文件应检出的安全类别
- **指标**: Precision / Recall / F1 / 延迟

## 推广判据

- ✅ **推广候选**: agentic 的 precision 提升（误报下降）且 recall 不下降
- ⚠️ **成本可接受**: 单 PR token 成本 ≤ 单次版的 3-5 倍
- ⚠️ **延迟可接受**: 平均工具调用次数 1-4 次，无频繁触顶

## 评测结果

> 运行 `python scripts/eval_agentic.py --real` 填入真实数据。

| 指标 | Single-shot | Agentic |
|------|-------------|---------|
| Precision | — | — |
| Recall | — | — |
| F1 | — | — |
| TP | — | — |
| FP | — | — |
| FN | — | — |
| Avg latency (s) | — | — |

## 详细分析

（每个 fixture 的 matched/extra/missed 详情）

## 结论

- [ ] 达标 → Phase 4 推广到其它 reviewer
- [ ] 不达标 → 保留 security 单次，agentic 代码留在 flag 后（默认关）
