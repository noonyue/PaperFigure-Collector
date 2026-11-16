# 开发路线：v0.2 → 可用于系统综述的正式版

## 现在已实现
- PMC Article Datasets AWS：DOI/PMID/PMCID、JATS、媒体文件、图片分类与哈希/审计。
- Springer Nature：正式 OA JATS API 查询、DOI 精确匹配、XML 原始图号图注；下载 XML 内实际声明且位于允许域名的绝对图片 URL。
- Elsevier/Cell Press：Object Retrieval view=META、同一 <ref> 对象按 IMAGE-HIGH-RES/其他版本分组、已授权图像下载；FULL 结构化全文有合法权限时按 locator 匹配图注。
- AAAS/Science：PMC 路径优先，出版社直连缺少可核实 API 时明确阻塞。
- 12 项测试，包括 2 项模拟端到端出版社 API 流程、无密钥阻塞、恶意 URL 检查与拒绝跳转。

## 必需补齐（重要性从高到低）
1. **出版版完整性复核**：合法获取期刊图件目录（VoR figure index）→ 与 JATS/META 的图号列表逐项匹配 → 报告缺失和版本差异。不能自动网页抓图或无权限访问。需要选择可合法调用的出版商官方 Figure API 或编辑流程提供的数据源。
2. **Springer Nature 高清图获取协议**：调研官方开发者平台是否提供 media asset URL 或其他正式授权机制。只在文档和许可确认后增加相对 URL 到官方 URL 的映射；不可根据 DOI/fig 编号编造资源路径。
3. **Elsevier 数据映射**：面对不同 XOCS XML 结构、Cell Press 的 graphical abstract 对象命名、Supplementary 的实际标记，建立不同期刊实际 response 的匿名测试样本；区分图像对象与非正文媒体。
4. **Science/AAAS 直连支持**：先取得 AAAS 官方开发者文档/合法接口与使用协议，才扩展独立 adapter；否则保留 PMC/本地文件路径。
5. **局部 PDF 兜底模块**：仅对用户合法取得的 PDF 做图框与图注识别，生成 PDF 裁剪图且明确标记 source_quality=pdf_crop，不要冒充原出版高清图片；避免 PDF 重排造成图号错位。
6. **实际在线验收**：每种出版社各 3 篇正式研究论文（开放获取，跨年份），人工确认 GA、Main Fig 1–N、Extended Data 是否齐全；并逐一记录 API 错误、下载耗时、尺寸/版本、许可和真实图注匹配率。
7. **可追溯 Excel 输出**：增加按文献、图片、失败记录分 sheet 的批量总表；允许合并到博士课题系统综述素材库。
8. **License/rights 强化**：图级第三方素材检测、图片来源版权说明、用户许可范围的严格提示与按许可证清单导出。

## 正式版阶段验收
- 真实期刊出版版完整性参考集合：每种出版社不少于 3 篇可合法访问样本。
- 与期刊 Figure Index 的匹配率、主图漏图率必须可量化，且 0 次将缩略图/裁图误标为原始高清原图。
- 每个图形摘要、正文 Figure、Extended Data 对应原始链接或明确缺失原因。
- 缺少许可时不下载；凭证不写入报告；绝不尝试规避访问限制。
- 输出文件能被 Excel 读取，SHA256 可复验，测试不依赖网站页面布局。
