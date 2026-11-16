---
name: paperfigure-collector
description: Archive authorized journal figures and audit the figure list against an independently sourced Version of Record index. Supports PMC, Springer Nature OA JATS, entitled Elsevier Object Retrieval and FULL XML, and local JATS with media.
---

# PaperFigure-Collector v0.3

Given a DOI, PMCID, PMID or authorized JATS with media, archive the available figure files and their source captions. Keep article versions, source URLs, figure labels, categories, file hashes and available resolution evidence.

## Acquisition

1. Use PMC Article Datasets first, selecting published PMC data when available. Record the selected PMC version and manuscript indicator. Use the Open Access Subset permissions for media acquisition.
2. For Springer Nature DOIs, use the OA JATS API with `SPRINGER_NATURE_API_KEY`. Match the primary DOI and use the allowlisted official absolute media addresses supplied by the XML. Record unresolved relative references in the audit.
3. For Elsevier DOIs, use entitled Object Retrieval META with `ELSEVIER_API_KEY` and applicable `ELSEVIER_BEARER_TOKEN` or `ELSEVIER_INST_TOKEN`. Download from the official HTTPS object endpoint. Use the documented rendition order, validate declared pixel dimensions and record any fallback. Match FULL figure captions, labels and roles to META by unique locators.
4. Use the PMC or local authorized JATS path for Science and AAAS.
5. Retain graphical abstracts, main figures and Extended Data. Enable supplementary figures when requested. Preserve unresolved object candidates for review.
6. Use source bytes and record SHA-256, dimensions, resolution tier, credited attribution and license where available. Report access and format failures from the documented interfaces. Keep credential values within the authorized requests.

## Version of Record audit

Provide `--vor-index` with a JSON list of articles, indexed by exact DOI. Each entry records a dated publisher figure index, publisher page or published PDF, the checked categories, and the figure labels. The examples directory contains a reference for nine articles.

Run a collection with the index or audit an existing manifest offline.

```bash
python scripts/publisher_adapters.py --id PMC13046474 --provider pmc --vor-index examples/verification_index.json --output collected
python scripts/vor_audit.py --manifest collected/PMC13046474/manifest.json --vor-index examples/verification_index.json
```

Report `counts.complete` for acquisition and `vor_audit` for independent label coverage and verification of saved file hashes. Include label provenance, missing or ambiguous figure records, unavailable files, extra records, lower-resolution renditions and source version. Identical figure labels in an author manuscript require version review. Use separate file-level evidence when establishing the precise publisher rendition.

For setup and the observed limitations, see [README](README.md) and [live validation](LIVE_VALIDATION.md).

## Official references

- [PMC Article Datasets](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/)
- [Springer Nature Open Access API](https://dev.springernature.com/docs/api-endpoints/open-access/)
- [Elsevier Object Retrieval](https://dev.elsevier.com/documentation/ObjectRetrievalAPI.wadl)
- [Elsevier Article Retrieval](https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl)
