---
name: paperfigure-collector
description: Download and audit legally accessible figures of peer-reviewed journal articles from DOI/PMCID/PMID or authorized JATS XML; categorize graphical abstracts, published main-text figures, extended-data and opt-in supplement figures; retain original bytes, captions, provenance, resolution tiers, and missing-image reports. Use for systematic literature-review figure archiving, NOT to circumvent paywalls or obtain reuse rights.
---

# PaperFigure-Collector v0.2

## Task contract

Given a paper DOI/PMCID/PMID or an authorized JATS XML and image directory, collect the best available licensed figure files; make one output directory per paper; preserve full JATS captions or only captions actually verified in the publisher XML; report missing sources and uncertainty. Do not confuse a successful download of some assets with completeness of the journal Version of Record (VoR).

## Always apply

1. Prefer PMC OA Article Datasets AWS with JATS XML. Prefer the publisher's published PMC version over an author manuscript when present; record source_version.
2. When PMC is unavailable, route 10.1038/, 10.1007/, and 10.1186/ DOI prefixes to the Springer Nature OA JATS API (API key required), 10.1016/ to Elsevier's official Object Retrieval API (verified entitlement and key required), and 10.1126/ to a PMC-only Science/AAAS fallback. Do not invent an AAAS image endpoint.
3. Never scrape publisher HTML pages, guess private image CDN URLs, bypass paywalls/captchas, or forward entitlement tokens to untrusted hosts/redirects. Publisher image requests are restricted to official allowlisted domains and do not follow redirects.
4. The absence of an API key, a zero-result full-text response, or an unresolvable image URL is a reported missing or blocked condition, never a success.
5. Recognize <fig>, <fig-group>, <graphic>, <alternatives>, <abstract abstract-type="graphical">, and <floats-group>; preserve separate categories graphical_abstract, main, extended_data, supplementary (optional), and unclassified (publisher objects whose role is not confirmed).
6. For Elsevier, group <attachment> entries by official <ref>; prefer IMAGE-HIGH-RES, then IMAGE-DOWNSAMPLED, then IMAGE-THUMBNAIL; match complete captions only when full-text XML refers to the same object locator. Do not assume API object lists exhaust article figure lists.
7. For Springer JATS, download figure media only when it provides an explicit absolute allowlisted official media URL. Report relative image refs as unresolved; do not reconstruct URLs from patterns.
8. Save source-media bytes unchanged, SHA-256, dimensions where recognizable, actual quality tier, source URL, figure ID/label, full caption, source version and license if known. Do not upscale, synthesize, or split figure panels.
9. Keep Graphical Abstract and Extended Data separate from body figures. Do not turn supplement PDFs into main figures. An OA article can still have separately credited third-party figures: a recorded license does not imply redistribution rights for every image.
10. Source XML count and complete indicate only structural completeness against the retrieved source; final use in review papers or a thesis requires manual publication-rights check and source-version verification.

## Run

~~~bash
python -m pip install -r requirements.txt
python scripts/publisher_adapters.py --id PMC10009416 --provider pmc --output ./collected
python scripts/publisher_adapters.py --id 10.1038/s41467-025-55825-x --provider auto --output ./collected
python scripts/publisher_adapters.py --csv ./papers.csv --column doi --provider auto --output ./collected
python scripts/publisher_adapters.py --local-xml ./article.xml --local-media ./media --output ./collected
python -m pytest -q
~~~

For publisher API access, use local environment variables (never paste keys into chat or shell command parameters): SPRINGER_NATURE_API_KEY, ELSEVIER_API_KEY, optionally ELSEVIER_BEARER_TOKEN and ELSEVIER_INST_TOKEN. An API key alone does not confer entitlement.

## Results and check

Per article, create available categories: main/, graphical_abstract/, extended_data/, supplementary/, and unclassified/. Also create manifest.json, figure_index.csv, captions.md, gallery.html, and audit.md. A batch adds batch_summary.json. The Springer route also saves article.xml.

Report the paper identifier, provider, source version, figure records, media files, absent/partial records, unsupported/permission-blocked status, whether any graphic is a lower-resolution fallback, and whether caption/VoR/rights verification is still needed. If the source itself might be incomplete, explicitly mark it so.

## Limitations

- PMC official dataset support inherited from v0.1. Springer OA JATS and Elsevier entitled-object adapters have mock-tested integration, not live credential-based publisher integration tests in the current environment.
- AAAS lacks an independently verified official direct-image adapter. Do not claim all Science figures unless PMC source and journal-specific comparison establish it.
- The Springer API often gives relative image refs without an authorized media URL; those remain unresolved rather than claiming a download.
- Elsevier object metadata alone does not prove published main-figure coverage or captions. Figure grN naming is a candidate classification unless publisher full text establishes it.
- No HTML scraping, PDF-extracted figures, or DOI-wide completeness proof in v0.2.

## Official references

- https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/
- https://dev.springernature.com/docs/api-endpoints/open-access/
- https://dev.elsevier.com/tecdoc_retrieval_request.html
- https://dev.elsevier.com/documentation/ObjectRetrievalAPI.wadl
- https://www.elsevier.com/about/policies-and-standards/text-and-data-mining/license
