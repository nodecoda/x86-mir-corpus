# Finite quotient

`quotient.py` answers a different question from `points`: not "which queries do
we emit?" but "how many behaviourally distinct queries does the rule set have?"

## Key, axis, length

- **Key** — the vector of observable outcomes over a dependency component. The
  outcome is the first matching rule index per opcode (`match`, `none`, or
  `unknown`). An unmodelled predicate stays `unknown`; it is never coerced to
  false.
- **Axis** — a rule read set. `legalFor` entries couple their columns; cartesian
  rules read one slot per list; predicate-gated rules read their predicate
  slots; mem-descriptor rules add `mem`. Slots read together form one axis
  (`slot0`, `pair(0,1)`, `triple(0,1,2)`, `pair(0,1)+mem`).
- **Length** — the size of the axis candidate universe, i.e. the product of the
  parsed class/mem/align vocabulary. The candidate universe is the full parsed
  class list, so class counts are exact for modelled predicates rather than a
  panel sample.

`unrepresented` counts quotient classes the target boundary panel never
touches, and `unrepresented_detail` lists one example per class with a reason:

- `panel_gap` — a modelled class the boundary panel simply misses; the next
  panel revision should add a representative.
- `opaque_predicate` — a class boundary read through a predicate the expression
  layer cannot reduce yet; the fix is modelling, not more points.

It is a panel-coverage diagnostic, not a pass/fail gate.

## Run

```bash
PYTHONPATH=src python3 scripts/quotient_report.py \
  --target aarch64 --config A2 \
  --repo /home/ubuntu/llvm/llvm-project \
  --out /tmp/quotient-a64-A2.json
```

Read-only. It never changes panels, corpora, or pinned revisions.

## Checked-in reports

| Report | Summary |
|---|---|
| `quotient-x86-C2.json` | 275 rules, 214 reached by a class tuple, 8 opaque builders |
| `quotient-aarch64-A2.json` | 616 rules, 364 reached, 32 opaque builders |

The quotient coverage counts rules reachable from a type class tuple alone.
The corpus is higher (582/611 active AArch64 rules) because rule witnesses add
predicate-driven shapes for opaque builders.  The remaining quotient gap is
shadowed rules plus lambdas the expression layer does not reduce yet;
`opaque_builders` names the latter explicitly.

## Limits

- Predicate calls inside lambda bodies are not recovered. The rule set is
  charged with every slot it could read (a coarser, sound quotient) and listed
  in `opaque_builders`.
- The config axis is reported as the selected config; run the tool once per
  config to compare them.
