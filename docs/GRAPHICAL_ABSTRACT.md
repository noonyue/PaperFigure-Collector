
# Graphical Abstract Acquisition Engine

## 采集和展示

现有 v0.3 的归档命令会同时输出专项结果 `graphical_abstract.json`。单独采集摘要图时使用 `ga_cli.py`。

```powershell
python scripts/ga_cli.py acquire --doi 10.1016/j.xcrm.2024.101715 --output out/graphical-abstract
python scripts/ga_cli.py gallery --run-dir runs/mitochondrial-stress-100-20261116
```

采集结果的 `gallery.html` 可离线打开。批量图库的 `paper_gallery.html` 按论文展示。每张卡片先显示图形摘要，其次显示正文图和 Extended Data。归档中存在补充图时，单独保留补充图分区。

## 来源和证据

| 来源 | 识别与取图依据 |
|---|---|
| PMC 或本地 JATS | 读取显式的摘要角色以及同一范围内的 `graphic`、`inline-graphic`、`media`、`fig`、`fig-group` 和 `alternatives`。JATS 图形引用只在对应论文的媒体目录内匹配。 |
| Elsevier | 将 FULL 的 `ce:abstract`、`ce:figure` 和 `ce:link` 的 locator 与 META 里的附件引用精确对应。若同一 locator 同时指向正文图和图形摘要，记为待核读。图件尺寸与 META 声明不符时尝试同版别的下一个媒体。 |
| Springer Nature | 使用 OA JATS 和出版社明确标注的网页区块。Springer 生产 XML 中主摘要内锚定的 `Figa` 结构由该出版社适配器处理。 |
| Taylor & Francis | 使用 T&F JATS 和官方网页中专用的图形摘要区块。 |
| Figshare | 校验关联条目、自身版权许可及明确的论文 DOI 关系。公有授权条目使用官方 API。已有机构或账号访问授权时，可设 `FIGSHARE_TOKEN` 并显式传入 `--authorized`。 |
| 出版 PDF | 按版别在原图来源未成功时读取授权 PDF。要求论文 DOI 和明确的 Graphical Abstract 标题。默认仅在标题对应范围内存在唯一图形时裁切，也可指定经人工核读的裁切矩形。结果标记 `pdf_crop`，同时记录页码、坐标和 DPI。 |

网页的社交卡片、封面、普通正文图和未明确标注的摘要插图进入候选记录。日志和图库显示均以论文内的证据为准。

### 可选来源

```powershell
# JATS 与对应本地原图
python scripts/ga_cli.py acquire --doi 10.1016/j.xcrm.2024.101715 --local-xml article.xml --local-media media --output out/from-jats

# PMC 未取得原图时，继续尝试已授权的出版社 API
python scripts/ga_cli.py acquire --doi 10.1016/j.xcrm.2024.101715 --publisher-api --output out/from-api

# 检查指定的官方出版页面以获取其标注的网页版
python scripts/ga_cli.py acquire --doi 10.1080/10717544.2024.2361168 --article-url https://www.tandfonline.com/doi/full/10.1080/10717544.2024.2361168 --output out/from-html
```

`--article-html` 可与 `--article-url` 一起传入已获授权的本地网页快照。`--figshare-id` 接收确认关联的数据条目编号。提供授权出版 PDF 时使用 `--pdf`、`--pdf-url`，需要定位时追加从 1 起算的 `--pdf-page` 和 `--pdf-rect X0 Y0 X1 Y1`。坐标以 PDF 页面左上为原点，单位为点。仅运行指定来源时可传 `--skip-pmc`。

Elsevier 和 Springer Nature 的凭证继续使用 v0.3 文档中的环境变量。

## 状态和归档字段

| 状态 | 含义 |
|---|---|
| `downloaded` | 至少一项媒体已归档。每个版别与面板仍保留自己的状态和尝试记录。 |
| `detected_not_accessible` | 来源确认存在摘要图，但缺少可用对象、权限或有效媒体。具体原因记录在结果中。 |
| `candidate_needs_review` | 找到候选，但角色、对应关系或版别需要核读。 |
| `not_found_in_source` | 已检查的来源中未找到明确标记。 |
| `confirmed_not_provided` | 独立出版目录的核查范围明示包含图形摘要，且该范围零项。可通过 `--vor-index` 载入。 |
| `unknown` | 尚无可验证的文章来源，来源失败原因随结果保留。 |

版别 `web_only`、`print_only` 和 `unspecified` 与文章来源版本分别记录。不同版别不会相互替代。同一版别内相同字节可复用一个文件，每项图注、来源和关联证明仍分别保存。原图文件记录识别出的格式、实际尺寸和单位、来源 URL、DOI、许可证、SHA-256 和来源版本。未披露的字段留空。

## 复跑验收

```powershell
python -m pytest -q
python scripts/validate_ga_live.py --output runs/ga-validation --probe-api
```

[真实公开样本定义](../examples/ga_live_cases.json)带有独立论文链接和期望结果。实际验证汇总在 `metrics.json`，源码端、媒体端、未获取原因、已发现对象的获取成功率及误分类分别计数。未登录 API 的 HTTP 响应另列。公开存档测试和凭证接口联调的范围见验收记录。
