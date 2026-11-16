# PaperFigure-Collector

A research-literature skill and Python CLI for collecting legally accessible journal figures, graphical abstracts, extended-data figures, captions, provenance, checksums, and missing-asset reports.

> **Status:** v0.2 prototype. The PMC route is implemented; Springer Nature and Elsevier routes are implemented against documented interfaces but have only been tested with mocked responses. Science/AAAS is currently handled through accessible PMC versions. This project does not promise universal access to every publisher figure or prove Version of Record completeness.

## Features

- Resolve DOI / PMCID / PMID inputs and batch CSV lists.
- Parse JATS figure structures, including graphical abstracts, body figures, Extended Data, and opt-in supplementary material.
- Prefer the best available publisher-provided image variant when the official API exposes quality tiers.
- Preserve source captions, figure labels, source URLs, source-version notes, SHA-256 hashes, dimensions, and download failures.
- Produce manifest.json, figure_index.csv, captions.md, gallery.html, and audit.md.
- Use official accessible sources and explicit publisher adapters; do not bypass paywalls, captchas, or access controls.

## Installation

Python 3.10+:

~~~bash
python -m pip install -r requirements.txt
python -m pytest -q
~~~

For Agent Skills-compatible clients, install this repository as a skill and follow SKILL.md.

## Quick start

~~~bash
# PMC article
python scripts/publisher_adapters.py --id PMC10009416 --provider pmc --output ./out

# Automatically route by DOI; use PMC first where available
python scripts/publisher_adapters.py --id "10.1038/s41467-025-55825-x" --provider auto --output ./out

# Batch DOI list
python scripts/publisher_adapters.py --csv ./examples/papers.csv --column doi --provider auto --output ./out

# Local JATS + media already available to you
python scripts/publisher_adapters.py --local-xml ./examples/sample_article.xml --local-media ./examples/media --output ./out

# Include supplementary figures in a separate folder
python scripts/publisher_adapters.py --id PMC10009416 --include-supplementary --output ./out
~~~

Example identifiers illustrate command syntax; they do not guarantee that a paper is present in an open dataset or that the user has access rights.

## Publisher API credentials

Set credentials in your local environment. Never commit API keys or tokens.

**PowerShell:**

~~~powershell
$env:SPRINGER_NATURE_API_KEY="your-key"
$env:ELSEVIER_API_KEY="your-key"
# Optional only where your Elsevier access method requires them:
$env:ELSEVIER_BEARER_TOKEN="your-authorized-token"
$env:ELSEVIER_INST_TOKEN="your-authorized-institution-token"
~~~

**Linux/macOS:**

~~~bash
export SPRINGER_NATURE_API_KEY="your-key"
export ELSEVIER_API_KEY="your-key"
~~~

An API key alone does not grant entitlement to paywalled full text or image files. The tool reports blocked or missing assets rather than attempting to bypass restrictions.

## Output

Each article receives its own folder with available categories:

~~~text
out/
  <paper-id>/
    main/
    graphical_abstract/
    extended_data/
    supplementary/
    unclassified/
    manifest.json
    figure_index.csv
    captions.md
    gallery.html
    audit.md
  batch_summary.json
~~~

The complete field describes completeness relative to the retrieved source records. It does not prove that every figure on the publisher website or in the Version of Record has been collected.

## Current source coverage

| Source | Current support | Important limitation |
|---|---|---|
| PMC Article Datasets | Official AWS dataset route, JATS, accessible media | Not every article is present; source version may differ from VoR |
| Springer Nature | OA JATS API route | Requires API key; relative media references are not guessed |
| Elsevier / Cell Press | Official Object Retrieval API route; prefers IMAGE-HIGH-RES | Requires applicable API access and content entitlement; object list may not exhaust article figures |
| Science / AAAS | PMC-accessible versions | No universal direct AAAS figure adapter is claimed |
| Local JATS | Offline parsing and audit | Requires user-provided lawful XML and media files |

## Tests

~~~bash
python -m pytest -q
~~~

The v0.2 development suite contains 12 offline/mock tests. It is not a substitute for live, credentialed tests against each publisher.

## Responsible use

- Respect publisher terms, licenses, institutional entitlements, and third-party figure credits.
- Downloading or archiving a figure does not grant permission to republish it.
- Do not scrape restricted publisher pages, guess private CDN paths, bypass paywalls, or follow untrusted redirects with credentials.
- Do not label PDF screenshots, thumbnails, or lower-resolution alternatives as original high-resolution publisher figures.

See SKILL.md, DEVELOPMENT_ROADMAP.md, and CODEX_BUILD_PROMPT.md.
