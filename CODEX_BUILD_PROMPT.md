# 给 Codex/Cursor 的下一阶段精简指令

目标：在现有 PaperFigure-Collector v0.2 上扩展适配器，最终达到可审计的跨出版社正文插图归档。**不得删除现有文件或改变既有命令行为。**

输入：scripts/collect_figures.py、scripts/publisher_adapters.py、所有测试、SKILL.md。

工作：
1. 用出版社正式文档和合法获取的真实公开样例验证 Nature/Springer、Cell/Elsevier、Science/AAAS 三个适配器；保留接口响应样例（脱敏）。
2. 优先实现 Fig. 1–N 与 Graphical Abstract 的全数比对；Extended Data 独立分类；Supplementary 默认关闭。
3. 对 PDF 兜底只处理用户合法提供的本地文件，明确标记 PDF 裁剪图及图注置信度。
4. 对 API 权限、解析失败、无法获取高清图、图片被重定向、图注缺失提供明确审计记录；API Key 仅从环境变量获取，任何输出不泄露。
5. 不要绕过付费墙、验证码或期刊访问控制；没有官方媒体接口时只报告缺失，不推测链接。

交付：可运行源码、更新 README.md/SKILL.md、实际接口测试+离线 mock 测试、版本化 ZIP、对 3 家出版社各不少于 3 篇公开论文的 Figure Index 对照表（若合法接口不可用，报告阻塞而不虚构）。

验收：全部测试通过；图件数、原图分辨率等级、图注、SHA256、来源版本、缺失原因在 manifest.json/figure_index.csv 可核查；图像的出版版完整性须经独立 Figure Index 核对，不能仅凭 API 成功断言。
