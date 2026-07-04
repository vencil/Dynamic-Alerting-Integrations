Scratch file for live-verifying PR #988 (validate.yaml required-checks fix) + PR #983
(docs-ci.yaml required-checks fix). This file is intentionally outside both workflows'
path filters. This PR exists only to confirm all required checks report skipped/success
and mergeStateStatus goes CLEAN without an admin bypass; it will be closed without
merging once verified.
