# Triage entry points

The symbol table fixes the mapping from a legalizer action to the
`LegalizerHelper` entry point that should explain a failure. The mapping is
data (`helper_entrypoints` in `dsl_symbols.json`), so it moves with the pin.

| `LegalizeAction` | Helper entry point |
|---|---|
| `Lower` | `LegalizerHelper::lower` |
| `Libcall` | `LegalizerHelper::libcall` |
| `WidenScalar` | `LegalizerHelper::widenScalar` |
| `NarrowScalar` | `LegalizerHelper::narrowScalar` |
| `FewerElements` | `LegalizerHelper::fewerElementsVector` |
| `MoreElements` | `LegalizerHelper::moreElementsVector` |
| `Bitcast` | `LegalizerHelper::bitcast` |
| `Custom` | target `legalizeCustom` / `legalizeIntrinsic` |
| `Legal` | no helper call |
| `Unsupported` | terminal failure |
| (dispatch) | `LegalizerHelper::legalizeInstrStep` — step loop, not an action |

`dsl report --json` prints `helper_entrypoints[].action`, which is the
authoritative spelling used by the sidecar's action rendering.
