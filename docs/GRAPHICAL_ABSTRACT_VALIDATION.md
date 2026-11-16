
# 图形摘要专项验收

2026-11-16 的公开样本分开验证论文级识别、媒体获取和类别错误。样本带独立出版证据链接，采用真实公开的出版版 PMC JATS、原始图件和出版 PDF。

| 指标 | 实测 |
|---|---:|
| 论文级识别准确率 | 9/9（100%） |
| 已发现图件的获取成功率 | 6/6（100%） |
| 图件类型或关联误分类率 | 0/9（0%） |
| 获取方式 | 5 个原图、1 个 PDF 裁切 |
| 未归档与待核读 | 1 篇有独立缺席证据、2 篇在核查来源未标注摘要图、1 篇另列的未标注插图待核读 |

九篇标注样本包含六篇有图形摘要和三篇阴性对照。另列的未标注 Springer 插图不参与准确率分母。两篇 Taylor & Francis 样本验证了 JPEG 和 WebP 原始文件，Elsevier、Springer Nature 与 JCI 样本验证了出版版结构和原图。Neural Regeneration Research 样本验证了双栏出版 PDF 的最后兜底。真实论文及其独立来源见 [样本表](../examples/ga_live_cases.json)。

Elsevier META、Elsevier FULL 和 Springer OA JATS 的无凭证在线探测均返回 HTTP 401。官方 API 的成功响应与权限边界在回归用例中覆盖，此次真实正样本使用公开存档。Figshare 的关联、许可、定向认证和拒绝下载路径经过测试。私有 Figshare 帐号没有做在线联调。

`python -m pytest -q` 的 52 个用例全部通过，其中 v0.3 原有 21 个。专项模块的行覆盖率为 84%。另用正常 v0.3 PMC 入口归档真实 Elsevier 论文，验证摘要图和正文图同时生成。验收目录的 `metrics.json` 保留逐篇证据和失败分类，`index.html` 可离线查看出版图件。
