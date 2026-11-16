# PaperFigure-Collector v0.3 在线验收

核读日期为 2026-11-16。Springer Nature、Elsevier / Cell Press、AAAS 各选 3 篇公开研究论文。归档使用 PMC Article Datasets，其中 AAAS 样本通过 `--provider science` 调用 PMC。

## 逐篇结果

“核验图件”统计在独立目录中匹配图号且归档文件 SHA-256 复验一致的图件。“有源图注”统计采集来源提供独立图注的图件。耗时为本次单篇命令的本地总耗时。

| 出版方 | 年份 | DOI | PMCID | 核验图件 | 有源图注 | PMC 许可证 | 秒 | 目录核查 |
|---|---:|---|---|---:|---:|---|---:|---|
| Springer Nature | 2025 | 10.1038/s41467-025-55825-x | PMC11739661 | 6/6 | 6/6 | CC BY-NC-ND | 7.06 | matched |
| Springer Nature | 2026 | 10.1038/s41563-025-02463-9 | PMC13046474 | 15/15 | 15/15 | CC BY | 13.95 | matched |
| Springer Nature | 2026 | 10.1038/s41586-026-10183-6 | PMC12979198 | 9/9 | 9/9 | CC BY-NC-ND | 9.13 | matched |
| Elsevier/Cell Press | 2022 | 10.1016/j.celrep.2022.110667 | PMC9035679 | 7/7 | 6/7 | CC BY-NC-ND | 12.44 | matched |
| Elsevier/Cell Press | 2023 | 10.1016/j.celrep.2023.113057 | PMC10616818 | 8/8 | 7/8 | CC BY-NC-ND | 12.46 | needs_review |
| Elsevier/Cell Press | 2024 | 10.1016/j.celrep.2024.114504 | PMC11290320 | 5/5 | 4/5 | CC BY | 8.59 | matched |
| AAAS | 2022 | 10.1126/sciadv.abn1832 | PMC9166400 | 7/7 | 7/7 | CC BY | 12.91 | matched |
| AAAS | 2023 | 10.1126/sciadv.adg4561 | PMC10208582 | 5/5 | 5/5 | CC BY-NC | 5.94 | matched |
| AAAS | 2024 | 10.1126/sciadv.adn9188 | PMC11290488 | 9/9 | 9/9 | CC BY | 15.09 | matched |

合计 71/71 个图件通过图号及归档文件校验，其中正文图 54 张、Extended Data 14 张、图形摘要 3 张。三个图形摘要在归档 JATS 中无独立图注。PMC 标记 `PMC10616818.1` 为作者稿，8 张的图号和文件校验均通过，论文状态为待复核。所有样本媒体保留 PMC 的分辨率证据等级 `source_resolution_unverified`。

## 出版版证据

- Springer Nature 和 AAAS 的目录来自公开出版 PDF。目录逐图记录 PDF 页码，Nature 的两篇样本纳入 Extended Data。
- Cell Reports 的目录来自 ScienceDirect 已发表文章的图号与图形摘要栏目。2024 年样本的图形摘要依据该文致谢中的明确说明。2023 年样本的 Fig. 2 另核读其出版 PDF 第 4 页。 [2022 年样本](https://www.sciencedirect.com/science/article/pii/S2211124722004193) · [2023 年样本](https://www.sciencedirect.com/science/article/pii/S2211124723010689) · [2023 年出版 PDF 镜像](https://www.researchgate.net/publication/373595698_Cerebellar_interneurons_control_fear_memory_consolidation_via_learning-induced_HCN_plasticity) · [2024 年样本](https://www.sciencedirect.com/science/article/pii/S2211124724008337)
- [目录 JSON](examples/verification_index.json) 包含核查范围和逐图证据位置。[逐图 CSV](examples/verification_results.csv) 包含版本、许可、实际尺寸、采集链接、图注可用性和 SHA-256。

## 接口验证

- PMC 官方元数据中的 `s3://pmc-oa-opendata/…` 已实测。通过 PMCID 运行九篇样本均取得 JATS 和图件。另以 `10.1038/s41467-025-55825-x` 实测 DOI 自动路由。ID Converter 在当前环境返回 403，NCBI ESearch 回退成功解析并完成 6/6 张图核查。 [PMC 数据格式](https://pmc-oa-opendata.s3.amazonaws.com/README.txt) · [NCBI DOI 查询字段](https://pmc.ncbi.nlm.nih.gov/about/userguide/)
- Elsevier 官方公开文档样例分别解析出 9 个 META 分辨率记录、3 个对象标识，以及 3 个 XOCS 正文图。接口回归测试覆盖 HTTP 对象地址升级、DOI 检查、图形摘要 locator、有图注而无对象的图，以及高分辨率候选尺寸不符时的降级。 `tests/fixtures` 中是以官方结构为基础的精简样例，图注、标识和像素值使用测试值。 [Object Retrieval 样例](https://dev.elsevier.com/payloads/retrieval/objectRetrievalResp.xml) · [Article Retrieval 样例](https://dev.elsevier.com/payloads/retrieval/articleRetrievalResp.xml) · [HTTPS 接口契约](https://dev.elsevier.com/documentation/ObjectRetrievalAPI.wadl)
- Springer OA JATS 与 Elsevier 的正式授权直连仍需对应凭证实测。Springer 的 JATS 路由、媒体白名单和相对路径报告已通过离线测试。AAAS 的线上验收使用 PMC。 [Springer OA JATS](https://dev.springernature.com/docs/api-endpoints/open-access/) · [Elsevier 授权范围](https://dev.elsevier.com/sd_article_retrieval_views.html)

## 复现

```bash
python -m pytest -q
python scripts/publisher_adapters.py --csv examples/verification_papers.csv --column pmcid --provider pmc --vor-index examples/verification_index.json --output out
```

批次预期保留一条作者稿待复核，命令退出码为 2。证据页可能需要交互打开，核查命令使用随附的目录。
