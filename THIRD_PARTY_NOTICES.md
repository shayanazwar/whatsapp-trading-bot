# Third-party notices

## Kronos

- Upstream: https://github.com/shiyu-coder/Kronos
- Pinned upstream source commit: `67b630e67f6a18c9e9be918d9b4337c960db1e9a`
- Upstream license: MIT, copyright ShiYu (2025).
- Upstream model: `NeoQuasar/Kronos-small`, pinned revision `901c26c1332695a2a8f243eb2f37243a37bea320`.
- Upstream tokenizer: `NeoQuasar/Kronos-Tokenizer-base`, pinned revision `f6c7de7b0490e422fde7a44bc73369e07115c445`.
- Verify each Hugging Face model card and license at download time. No weight files are included in this source archive.
- `app/forecasting/kronos.py` contains a narrowly scoped adaptation of upstream autoregressive sampling so the individual sample dimension is returned rather than averaged. The upstream model/tokenizer implementation is installed separately under `third_party/Kronos`; the adaptation is explicitly identified in source comments. Retain the upstream MIT license and copyright notice in any redistributed build containing that code.

## Day Trader Skill

- Research reference only: https://github.com/Jakeschincariol/day-trader-skill
- Its MIT-licensed README and workflow concepts were consulted for feature decomposition (scanner, entry/exit planning, simulation, risk and budgeting). Its source code, scripts, broker adapter and UI are not included or copied into this application.

## Other runtime packages

See `pyproject.toml` and `frontend/package.json` for pinned package versions and their individual licenses.
