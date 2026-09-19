# Local container development

The image preserves the upstream `vuegraf` entrypoint, default configuration
path `/opt/vuegraf/conf/vuegraf.json`, and UID/GID 1012. Build and runtime both
use Python 3.13. The build context excludes credentials and local data.

Start OrbStack, then use its explicit local Docker context:

```sh
docker --context orbstack compose -f compose.local.yaml build
docker --context orbstack compose -f compose.local.yaml run --rm vuegraf
docker --context orbstack run --rm --network none --entrypoint python \
  vuegraf-telemetry:local -c 'import vuegraf.telemetry, vuegraf.telemetry_history; print("Telemetry modules loaded")'
```

The local Compose service prints CLI help and exits successfully. It is a
packaging smoke test, not a continuously running collector.

For local integration tests, mount a private configuration directory at
`/opt/vuegraf/conf` and use local InfluxDB settings. Never bake credentials into
the image. Enable the telemetry block documented in [TELEMETRY.md](TELEMETRY.md).
Do not use production database settings for local tests.

The full local stack follows production's config-init flow and service versions.
See [INTEGRATION.md](INTEGRATION.md): prepare inputs with
`ruby scripts/prepare-local-stack.rb` and use `int/stack.env` for that stack.

After local tests pass, build for the Ubuntu server's architecture:

```sh
docker --context orbstack build --platform linux/amd64 -t vuegraf-telemetry:amd64 .
docker --context orbstack run --rm --platform linux/amd64 --network none \
  vuegraf-telemetry:amd64 --help
```

No image is automatically published or deployed. A collection-progress
heartbeat health check remains a separate improvement before automatic healing;
a process-only check cannot detect a stalled collection loop.
