# Import route (`talos.importers`)

Turns a tag export the plant already has into a Talos plant map (`talos.map/1`) the gate can use:

| input | adapter | notes |
|---|---|---|
| I/O list / tag list CSV (any columns: tag, description, units, range lo/hi, alarm lo/hi, address, signal type, equipment) | `iolist:PATH` or `csv:PATH` | column names matched by synonym |
| Studio 5000 / RSLogix tag export CSV | `logix:PATH` | ExternalAccess -> ro/rw |
| Ignition tag export (JSON or CSV) | `ignition:PATH` | engUnit/engLow/engHigh/readOnly |

```
talos-importer map --from iolist:tests/fixtures/tag_export_fixture.csv --out map.json
talos-importer --llm http://127.0.0.1:8000/v1 map --from logix:tags.csv --out map.json   # LLM for the uncertain rows
```

`python -m talos.importers` is the same entry point.

Pipeline: adapter -> `TagRow` -> rule classifier (`classify.py`: class, safe direction, writable-while-running,
equipment role, confidence) -> optional LLM pass for rows under the confidence threshold (`llm.py`: any
OpenAI-compatible chat endpoint, `--llm URL` or `$TALOS_LLM_BASE` / `$TALOS_LLM_MODEL`, batched JSON answers cached
under `--cache`, `classified_by` recorded per register) -> plant map (`plantmap.py`: hashed names, addresses parsed
from the export or synthesised, validated by `talos.mapformat.validate`).

Privacy: a plant map carries sha1[:10] hashes of tag names only. Tag names, descriptions, site, company and
addresses of other systems never leave the adapter; only name/description/unit/type/range go to the LLM when one
is configured, and nothing goes anywhere when it is not (`--llm none`, the default).

Known limits:

- `build_plant_map` synthesises 4xxxx/3xxxx addresses when the export has none; these registers are flagged
  `synthetic_address` and should be bound to real addresses before the map is used.
- No OPC UA address parsing yet (`ns=..;s=..` rows get synthetic Modbus addresses).
- Historian-only exports have no write points and come out all-diagnostic, which is honest but read-heavy.
- When an export has one asset id for everything, assets are regrouped by name prefix (2 tokens).
