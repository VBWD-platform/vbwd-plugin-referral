# vbwd-plugin-referral

> VBWD referral coupons plugin (S92 Track B)

**Type:** Backend plugin · **Host app:** `vbwd-backend` · **Plugin:** `referral`

Part of the [VBWD platform](https://github.com/VBWD-platform). This repository is one
plugin in the modular VBWD SaaS marketplace platform; the core is intentionally
agnostic and gains this functionality only when the plugin is enabled.

## Install

Clone into the backend plugin directory and enable it:

```bash
git clone https://github.com/VBWD-platform/vbwd-plugin-referral.git vbwd-backend/plugins/referral\n```\n\nThen register it in `plugins/plugins.json` (`"referral": { "enabled": true }`)\nand add any config to `plugins/config.json`. The plugin follows the standard\nlayered layout (`routes` → `services` → `repositories` → `models`) and exposes a\n`BasePlugin` subclass in `__init__.py`.

## Versioning & changelog

Releases are tagged (e.g. `v26.6`); see [`CHANGELOG.md`](./CHANGELOG.md).

## License

Business Source License 1.1 — see [`LICENSE`](./LICENSE). Free for commercial
use while annual VBWD-attributable sales stay below the value of 6.7 BTC for the
reporting year; above that, a commercial license is required.
