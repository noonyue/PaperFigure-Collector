# PaperFigure-Collector v0.3

按 DOI、PMCID、PMID 或已有 JATS 归档论文图件。正文图、图形摘要、Extended Data 和补充图分别保存。输出记录源版本、图号、图注、网址、尺寸、已知分辨率等级、SHA-256 和缺失原因。

## 安装

Python 3.10+。在项目目录运行以下命令。

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

## 归档和出版版核查

```bash
# 通过 PMCID 归档公开图件
python scripts/publisher_adapters.py --id PMC10009416 --provider pmc --output out

# 根据 DOI 先查询 PMC，再按出版社路由
python scripts/publisher_adapters.py --id 10.1038/s41467-025-55825-x --provider auto --output out

# 使用公开样本和随附的九篇出版版图件目录复跑核查
python scripts/publisher_adapters.py --csv examples/verification_papers.csv --column pmcid --provider pmc --vor-index examples/verification_index.json --output out

# 复核已有归档和实际文件，全程离线
python scripts/vor_audit.py --manifest out/PMC13046474/manifest.json --vor-index examples/verification_index.json

# 使用已有 JATS 和本地媒体
python scripts/publisher_adapters.py --local-xml examples/sample_article.xml --local-media examples/media --output out
```

用 `--include-supplementary` 纳入补充图。已有归档缺少 DOI 字段时，可给离线核查命令传 `--doi`，已有 DOI 时两者须一致。

## 图形摘要专项采集

[Graphical Abstract Acquisition Engine](docs/GRAPHICAL_ABSTRACT.md)提供独立取图、按版别记录和来源核查。常规采集的论文图库也按图形摘要、正文图与 Extended Data 分区。可从已有批量归档生成按论文排列的离线图库。

## 出版版图件目录

`--vor-index` 接受一个含多篇论文的 JSON。每篇填写 DOI、来源类型、HTTPS 证据链接、核读日期，以及已核读的类别和图号。类别可选 `main`、`graphical_abstract`、`extended_data`、`supplementary`。核读之后如确认某类别有零张，仍在 `categories` 声明该类别。`locator` 可填写 PDF 页码或出版页面位置。单图另有证据时可填该图的 `evidence_url`。

```json
{
  "articles": [
    {
      "doi": "10.1038/s41467-025-55825-x",
      "source": {
        "kind": "publisher_pdf",
        "url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC11739661.1/PMC11739661.1.pdf",
        "checked_on": "2026-11-16"
      },
      "categories": ["main"],
      "figures": [
        {"category": "main", "label": "Fig. 1", "locator": "PDF page 3"},
        {"category": "main", "label": "Fig. 2", "locator": "PDF page 5"},
        {"category": "main", "label": "Fig. 3", "locator": "PDF page 7"},
        {"category": "main", "label": "Fig. 4", "locator": "PDF page 8"},
        {"category": "main", "label": "Fig. 5", "locator": "PDF page 10"},
        {"category": "main", "label": "Fig. 6", "locator": "PDF page 11"}
      ]
    }
  ]
}
```

来源类型还支持 `publisher_figure_index` 和 `publisher_page`。核查基于录入的目录，证据链接供回查。核查会读取归档文件并重算哈希。

| 字段或状态 | 含义 |
|---|---|
| `counts.complete` | 当前 JATS 或对象数据源内的采集状态 |
| `vor_audit.status=matched` | 已声明类别内图号一一匹配，归档文件通过哈希核验 |
| `incomplete` | 参考图号缺失，或匹配记录的媒体缺失、部分缺失或哈希异常 |
| `needs_review` | 有额外或重复图号、来源标签不明确、作者稿版本差异或采集错误 |
| `not_checked` | 缺少独立目录、论文 DOI 或对应 DOI 条目 |

带 `--vor-index` 的采集仅在每篇的采集状态完整、目录状态为 `matched` 时返回退出码 0。其余核查结果返回 2 并保留报告。图件内容版本和最高分辨率需要相应的出版商证据。旧归档缺少图号来源时，结果保留为待复核。

## 来源与凭证

| 来源 | 采集路径 |
|---|---|
| PMC Article Datasets | 支持公共 S3 元数据内的 `s3://pmc-oa-opendata/…` 与 HTTPS 地址，媒体按 PMC 开放集合及许可证标记获取 |
| Springer Nature | OA JATS 按文章 DOI 匹配，图片使用 XML 明示的官方绝对地址，相对路径记为未解析 |
| Elsevier / Cell Press | Object Retrieval META 提供候选附件与分辨率。FULL 的 JATS 或 XOCS locator 提供图号、角色和图注。优先获取高分辨率，已声明尺寸须与实际图像一致 |
| AAAS / Science | `--provider science` 使用 PMC 路径。出版图号可用独立目录比对 |
| 本地 JATS | 使用所给 XML 与媒体文件，读取 XML 中实际存在的 DOI 和许可声明 |

`--provider springer` 读取 `SPRINGER_NATURE_API_KEY`。`--provider elsevier` 读取 `ELSEVIER_API_KEY` 和适用的 `ELSEVIER_BEARER_TOKEN`、`ELSEVIER_INST_TOKEN`。在本机环境中配置凭证，内容访问范围以开发者账号的授权为准。

## 输出

- `manifest.json` 包括图件和媒体记录、源内采集状态，以及独立比对的 `vor_audit`。
- `figure_index.csv` 逐个归档文件记录图号、图注、尺寸、SHA-256、分辨率等级和错误。
- `audit.md` 记录已核查范围、缺失项、额外项、版本和授权状态。
- `captions.md`、`gallery.html` 供本地查看。Springer 路线另存媒体链接已脱敏的 `article.xml`。
- `batch_summary.json` 汇总本次运行的源内采集状态与出版版目录核查状态。

真实样本、证据来源和限制见[在线验收记录](LIVE_VALIDATION.md)。逐图数据见[验收明细](examples/verification_results.csv)。
