# Golden category set — ground truth for categorisation accuracy

`golden_category_truth.csv` is a labelled ground-truth set for measuring
categorisation *label* accuracy (the axis the balance gate does not cover).

- **136 real transactions**, sampled BLIND from the held statements (stratified
  by keyword so the rare/tricky categories appear — lenders, penal, interest,
  bounce, cash, salary — plus a random tail), then labelled independently
  against the SME-lending taxonomy in CLAUDE.md, applying its precedence rules
  (penal-before-interest, interest-by-sign, service-fee-is-not-penal,
  lender-name -> EMI/disbursal, and the MAB-inside-a-VPA false-positive trap).
- Labels are **AI-judged** (by the assistant acting as an independent judge) and
  are a STARTER set pending the domain owner's review — correct any label and it
  re-measures on the next run. `Category` is the judged truth; `Bank` is context.

Run it through the harness:

    BSA_CATEGORY_TRUTH=$(pwd)/tests/data/golden_category_truth.csv \
        .venv-test/bin/pytest tests/test_categorization_accuracy.py -s

The categorizer currently agrees with the judge on **57%** of these rows. The
biggest gaps it surfaced: `return / refund` is not detected at all (0/17),
`Interest received` misses glued/FD forms (2/10), ACH/NACH and UPI-mandate rows
are not tagged `ECS transaction` (1/9), and dividends are not recognised
(`Investment return credited` 0/4). A few disagreements are definitional (a
lender debit as EMI vs Interest-payments) and want a domain-owner ruling.
