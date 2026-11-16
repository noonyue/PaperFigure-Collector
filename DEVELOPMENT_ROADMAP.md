# PaperFigure-Collector 开发路线

## v0.3 已完成

- 可加载独立出版版图件目录，按 DOI、类别和图号逐项核对，复验归档文件的 SHA-256。结果覆盖缺图、缺文件、额外记录、歧义和作者稿版本差异。
- 修复 PMC 公共 S3 URI、Nature 附录 Extended Data 和 Cell Press 无图号图形摘要的解析。
- 验证 Elsevier 官方 META 与 FULL 样例，兼容旧的 HTTP 对象地址和实际 XOCS 命名空间。下载按分辨率等级与实际尺寸校验。
- 使用 Springer Nature、Elsevier/Cell Press、AAAS 各 3 篇论文做实际 PMC 下载和独立目录比对。结果见[在线验收记录](LIVE_VALIDATION.md)。

## 后续验证与开发

1. 使用正式开发者凭证实测 Springer OA JATS 的媒体覆盖，记录不同期刊的显式媒体地址和分辨率。
2. 使用已授权的 Elsevier 访问验证 Cell Press 各刊的实际 META 与 FULL 响应，补充图形摘要和补充图的真实结构样本。
3. 在获取 AAAS 官方接口文档与授权后评估直连适配。当前通过 PMC 或已取得的本地 JATS 采集。
4. 对已取得的本地 PDF 增加图框和图注提取，导出时标注 `source_quality=pdf_crop` 和图注置信度。
5. 增加按文献、图件和失败记录分表的 Excel 输出，以及图级版权及第三方素材标注。

完整覆盖以独立目录逐项匹配为准。精确出版媒体版本以出版商的文件级证据为准。
