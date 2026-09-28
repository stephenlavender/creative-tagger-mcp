# Agent instructions

This repository is public. Competitor-data examples and fixtures—including
Meta Ad Library page names, ad copy, and spend figures—must use obviously
synthetic brands such as "Everwell Labs (Demo)", never real brand names or
real `ads_archive` rows.

Treat hosted MCP, the published stdio package, and unreleased source as
different discovery surfaces. Never infer an MCP tool from a REST route name.
PyPI `0.2.4` is the current published 43-tool stdio release; `main` is the
unreleased `0.2.5` source surface with 47 tools and 10 prompts. Brain report
blocks and immutable snapshots are REST/dashboard routes, not MCP tools.

The deployed API starts one included history import automatically after the
first successful sync for each entitled connected workspace/ad account. Treat
CPA as spend per measured purchase and CPL as spend per measured lead; never
substitute pooled `conversions` when either outcome family is absent.

CPA means cost per measured purchase and CPL means cost per measured lead.
Never infer either denominator from pooled `conversions`, and never turn an
unmeasured purchases/leads value into zero. A successful Meta sync can start
the paid workspace's automatic included history import; describe it as one
import per connected workspace/ad account, not one per subscriber.
