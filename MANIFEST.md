# MEP Manifest

`mep-manifest.json` is an example template, not a universal default.

Before using it, change these fields for your own node:
- `alias`
- `capabilities.models`
- `transport.hub_url`
- `transport.ws_url`
- `auth.key_path`
- `runtime.openai_base_url`
- `runtime.openai_model`

## Purpose

The manifest gives a node one machine-readable config file for:
- alias and identity metadata
- hub and WebSocket endpoints
- heartbeat policy
- auth key location
- advertised skills and models
- provider runtime defaults

Environment variables still override manifest values when both are present.

## Example startup

```powershell
$env:MEP_MANIFEST_PATH="C:\path\to\MEP\mep-manifest.json"
python -m clients.adapters.mep_codex_provider
```
