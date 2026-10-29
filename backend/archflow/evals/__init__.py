"""Offline evaluation of ArchFlow's AI features against golden datasets (``archflow-eval``).

Datasets live in ``evals/datasets/*.yaml`` (one suite per file: compare, ask, command, design, suggestions,
review, lld, and adversarial, the red-team cases run against the other agents).
Every case runs the real agent through the gateway (a provider key is needed) and is scored by:

- deterministic checks from the case's ``expect`` block (recommended option, components added, mentions,
  ops that apply cleanly, grounded suggestions, ...), and
- RAGAS metrics (``--ragas``; see ``runner`` for how to install it), judged by the same gateway: faithfulness
  and response groundedness of answers against the project data, answer accuracy against a reference answer,
  and aspect critics ("technically sound") for edits and suggestions.

Reports record the prompt versions used, so ``--prompt compare.evaluate=v2 --baseline reports/v1.json`` is an
A/B test of a new prompt version. See ``runner`` for the command line.
"""
