# Light Programmer for HomeKit

See your [light-programmer](https://github.com/dongnh/light_programmer) lights' health from the Home app.

## What it is

One Contact Sensor per light, plus one for the whole system — the entire interface. Each light appears under the name you gave it; **Closed** means the system can reach it, **Open** means it dropped off (the standard contact-sensor convention: Open is the alert state). A **"Light Programmer"** system sensor reports the daemon itself. Turn on notifications to hear the moment a light — or the daemon — goes quiet.

<img width="775" height="734" alt="image" src="https://github.com/user-attachments/assets/30ab8cdf-060e-4060-8222-6d05e81dacf6" />

## How it works

The bridge holds no state. Every poll is one HTTP call to light-programmer's `/lights`, mirrored onto the sensors. Each light's HomeKit id is derived from the light id, so notifications and automations stay bound to the right light across restarts. The accessory set is fixed at pairing time — add a light to light-programmer and restart the bridge to surface its sensor.

## Requirements

macOS or Linux on the same LAN as an Apple Home hub, Python 3.10+, and a reachable light-programmer 0.15+ (the release that serves `/lights`).

## Install & pair

Install into a virtualenv and run with a config file pointing at your light-programmer. On first launch the bridge prints a HomeKit setup code; in the Home app choose Add Accessory and enter it. Set a fixed `pincode` so that code stays stable across restarts.

## Configure

A small JSON file — see [`examples/config.json`](examples/config.json). Fields you might set:

- `programmer_url` — light-programmer's mode HTTP endpoint.
- `programmer_api_key` — match light-programmer's `X-API-Key` if it requires one; empty for a loopback programmer.
- `bridge_name` — the name in Apple Home; it also seeds the stable MAC, so pick one you'll keep.
- `pincode` — fixed setup code (`DDD-DD-DDD`). Without it the bridge generates a new code on every restart.
- `poll_interval` — seconds between `/lights` polls.
- `fail_threshold` — consecutive failed polls before the system sensor flips to Open (debounces a transient blip).
- `notify_prefix` — text prepended to each light's name (default `"Communication to "`; set empty for bare names).

The light names themselves come from light-programmer's config, not here.

## License

MIT
