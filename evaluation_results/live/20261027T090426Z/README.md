# Live evaluation, 2026-10-27T09:38:05+00:00

Real model calls (billed). One run per case and approach, no repetitions: differences of one or two cases
are noise. Gold labels were loaded only after every investigation had finished.

## Provenance

- Code: commit `090c49779421f6686cae138f59f4df513ed77466` with uncommitted changes
- Model: `gpt-4o-mini`; limits: llm_max_input_tokens 64000, llm_max_output_tokens 4000, llm_timeout_seconds 120.0, agent_max_critic_rounds 2, agent_max_evidence_requests 3, investigation_deadline_seconds 300.0
- Budget $3.00; estimated spend $0.1345; not stopped
- Pricing: {"input_per_million": 0.15, "output_per_million": 0.6, "source": "https://developers.openai.com/api/docs/models/gpt-4o-mini", "checked": "2026-10-27"}

## Outcomes

- **multi_agent**: 10/10 completed (0 not run); strict 3/10, lenient 5/10, wrong 0/10; outcomes {'correct': 2, 'correct_inconclusive': 1, 'acceptable_inconclusive': 2, 'missed_inconclusive': 5, 'wrong_conclusion': 0, 'failed': 0, 'not_run': 0}
- **single_agent**: 10/10 completed (0 not run); strict 2/10, lenient 4/10, wrong 1/10; outcomes {'correct': 1, 'correct_inconclusive': 1, 'acceptable_inconclusive': 2, 'missed_inconclusive': 5, 'wrong_conclusion': 1, 'failed': 0, 'not_run': 0}

## Limitations

- 10 small synthetic cases, one run each; no statistical significance.
- Costs are estimates from reported token usage at standard prices.
- Accuracy is scored by keyword patterns in the gold labels (docs/EVALUATION.md).

Files: `results.json`, `per_case.csv`, `report.md`; checksums in `SHA256SUMS`.
