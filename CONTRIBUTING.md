# Contributing

Use Python 3.11+ in a fresh environment:

```sh
python -m pip install -e . -r requirements-dev.txt
python -m ruff check roomrill tests scripts
python -m ruff format --check roomrill tests scripts
python -m unittest discover -s tests -v
python -m roomrill.scenarios --cases examples/scenarios-v1.json --output reports/new-scenario-run
node --check roomrill/assets/app.js
```

Choose a new output directory for each scenario run. Original authored case bytes and earlier failures stay unchanged. A changed requirement needs a separate version and explanation; do not rewrite expectations to make an implementation pass.

For a bug, include the smallest synthetic event sequence, expected state/version/receipt and actual result. Distinguish fixture time from wall time and simulator behavior from hardware. Add a regression for a substantive invariant, not a counter or implementation detail. Model fixtures must be labelled; a canned JSON response is not a live-model test.

Keep changes focused. Preserve manual/external revisions, command identity, ownership fences and prior receipts across restart. Do not attach private ledgers, credentials, occupant data or an actual installation's device inventory to a public issue.
