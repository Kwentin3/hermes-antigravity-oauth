# Truncated tool-call diagnostic and bounded fix

Incident: Dual Review dr-4697d793fabc4e0c, Gemini child 20261021_210659_2c15c6, gemini-4-argon-medium via antigravity-oauth. The first four requests and source reads completed. Request five returned 7,879 reported output tokens in about 91 seconds. Hermes selected truncated_unreported and did not dispatch the malformed call; no final submission was acknowledged. The original pre-adapter result event/argument bytes are not retained in the inspected evidence. Token count alone does not establish an output cap.

## Confirmed independent bug

collect_completion fallback extraction unconditionally rewrote finish_reason to tool_calls after extracting a textual tool envelope. This erased an explicit length stop when a stream without preconfigured tools carried a closed envelope containing an incomplete inner argument string. Preserve the explicit stop instead. The patch does not repair or execute malformed JSON, mark incomplete reviews successful, retry models, or change reviewer settings.

Applicability limit: this fallback-specific defect is reproduced, but has NOT been established as the cause of the incident. With tools configured, the normal extraction path already preserves length. A SUCCESS envelope with a malformed inner argument string can also reach Hermes validation without a reported cap; the existing safe rejection is not itself a parsing bug. Upstream loss vs malformed generation cannot be distinguished without original boundary evidence.

## Verification

Regression first failed with tool_calls != length on baseline 85391b0102877d6c4dafd486b3600ecc372deb4f, then passed with the patch. Four targeted output-limit tests pass:

PYTHONPATH=/opt/hermes PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_antigravity_plugin.py -k output -v

Full suite: candidate 317 tests; baseline 316. Both show the same 15 failures, 6 errors, 5 skips; programmatic comparison found zero candidate-only failures. Thus the full suite is NOT green. Logs retained under /opt/data/cache/scratch/agy-truncation-{tests,baseline-tests}.log on the diagnostic host. Baseline and candidate were executed separately with identical PYTHONPATH and environment. Host/environment dependencies and timing assertions remain out of scope.

Live probe from this shell could not start: existing CLI auth discovery returned unauthenticated. No login, credential changes, settings edits, reviewer relaunch, or model substitution attempted. This does not prove the running Hermes provider lost auth; its prior successful route had a different execution context. A live test on the actual authorized route and incident-specific root-cause confirmation remain OPEN.

## Deployment boundary

Only a separate working clone was modified. Installed plugin, core Hermes and Dual Review configuration are unchanged. Do not reload a plugin during a running review. Source fix readiness, live verification and deployment are separate facts. Do not report the original failed review fixed or recovered from these tests.
