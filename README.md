# monomi

物見 — _your watchtower, always watching._

A small, fast, browser-accessible status page and resource monitor for Linux. Clearly inspired by [btop](https://github.com/aristocratos/btop), but built to live on a small case-mounted screen or get pulled up in a tab whenever you want to see how your box is doing.

![monomi-dashboard](https://files.catbox.moe/o62kam.png)

## What it is

You run a tiny Python collector on the machine you want to watch. Every second it bundles CPU, memory, swap, disk, network, processes, services, and a handful of static system facts into a JSON snapshot and POSTs it to a Cloudflare Worker. The Worker keeps the latest snapshot plus a 6-minute rolling history in a single Durable Object and serves a self-contained HTML/CSS/JS page that reads from it. Your browser polls once a second and repaints.

That's the whole thing. No database, no Docker, no Python web framework, no JavaScript build step on the host. About 2,600 lines of code total.

## Features

- **Live 1Hz updates** with sub-100ms repaint, no flicker
- **Braille graphs** in the spirit of btop, with water-reflection mirror mode for CPU and network
- **Per-core sparklines** coloured green to red along btop's CPU gradient
- **Memory + swap as bucket fills** showing each metric as a filling glass
- **Per-pool disk I/O sparkline** (read on top, write on bottom)
- **Multi-interface network**, with LAN IP plus public IP (blurred until hover for privacy)
- **Top 40 processes** by CPU, sorted live
- **System info footer**: OS, kernel, model, arch, GPU, shell, package count, last `apt` run, UPS state
- **Responsive layout** that holds up from 1920px down to mobile
- **No data leaves your edge.** Worker is in your Cloudflare account, history lives in your Durable Object, the page polls only your endpoint.

## Why

I wanted a glanceable status page for my home Pi 5 that I didn't have to SSH in to check. btop is gorgeous but it's a TTY app. Existing web monitors (Glances, Netdata, Cockpit, etc.) felt heavyweight: Python web stacks, gigabytes of historical metrics, paid tiers, big install footprints. I wanted the smallest possible thing that looked good, updated live, and could leave open on a small case display without melting the host.

## How it works

```
   ┌──────────────┐                        ┌──────────────────┐
   │  Linux box   │  POST 1Hz, Bearer auth │   Cloudflare     │
   │              │ ─────── JSON ───────▶  │      Worker      │
   │  collector   │                        │                  │
   │  (Python 3,  │                        │  Durable Object  │
   │   stdlib)    │                        │  latest + 360s   │
   └──────────────┘                        │     history      │
                                           └────────┬─────────┘
                                                    │  GET /  +  /api/stats
                                                    ▼
                                           ┌──────────────────┐
                                           │   Any browser    │
                                           │   polls 1Hz      │
                                           └──────────────────┘
```

The collector is stateless. The Worker is one file plus three inlined assets. The browser is vanilla JS, no framework, no build step it needs to know about.

## Tech stack

| Layer | Tech |
| --- | --- |
| Collector | Python 3, standard library only (no `pip install`) |
| Edge | Cloudflare Workers + a single Durable Object |
| UI | ~780 lines vanilla JS, ~640 lines CSS, ~160 lines HTML — zero runtime deps |
| Wire format | JSON over HTTPS, Bearer-token authenticated |
| Cadence | 1 Hz push, 1 Hz browser poll |
| Service unit | systemd (`Type=simple`, restart on failure) |

## Footprint

| Resource | Cost |
| --- | --- |
| Collector RSS on the host | ~25 MB |
| Collector CPU at 1 Hz | <1% on a Pi 5 |
| Disk usage | journald log lines only |
| Cloudflare Worker requests | ~170k/day per viewer (push + poll) — well inside the free tier |
| Durable Object storage | <100 KB per host |
| Page weight | one HTML doc, one JS file, one CSS file, all inlined into the Worker |

The whole project is built around the constraint that nothing should consume more than a sliver of resources.

## Quick start

You need a Cloudflare account (free tier is fine), Node 18+, and SSH to whatever Linux box you want to monitor.

### 1. Deploy the edge

```sh
git clone https://github.com/<you>/monomi.git
cd monomi/worker
npm install
npx wrangler login
npx wrangler secret put INGEST_TOKEN     # paste a long random string
npx wrangler deploy
```

Wrangler will print the URL, something like `https://monomi.<sub>.workers.dev`. Hit it in a browser to confirm the page loads (it'll show `--` everywhere until the collector starts pushing).

### 2. Install the collector

From your laptop:

```sh
cd ../collector
scp collector.py monomi-collector.service user@host:/tmp/
```

On the host:

```sh
sudo install -d /opt/monomi /etc/monomi
sudo install -m 0755 /tmp/collector.py /opt/monomi/
sudo install -m 0644 /tmp/monomi-collector.service /etc/systemd/system/monomi-collector.service

sudo tee /etc/monomi/collector.env >/dev/null <<EOF
INGEST_URL=https://monomi.<your-sub>.workers.dev/ingest
INGEST_TOKEN=<the same long random string from step 1>
INTERVAL=1.0
EOF
sudo chmod 600 /etc/monomi/collector.env

sudo systemctl daemon-reload
sudo systemctl enable --now monomi-collector
```

Refresh the page in your browser. The dashboard starts ticking immediately.

Total install time: under five minutes.

## Configuration

`/etc/monomi/collector.env`:

```
INGEST_URL=https://monomi.<sub>.workers.dev/ingest
INGEST_TOKEN=<random>
INTERVAL=1.0                              # push cadence in seconds
PIRONMAN_URL=http://127.0.0.1:34001/...   # optional, for Pironman 5 case stats
NUT_UPS=eaton3s                           # optional, NUT UPS name
```

The watched-services list lives at the top of `collector/collector.py`. Edit it for whatever you care about (`ssh`, `docker`, `nginx`, `postgres`, ...).

## Compatibility

monomi was built on a Raspberry Pi 5 in a Pironman 5 case, but the bulk of what it reads is just standard Linux:

| Reads from | Where it comes from |
| --- | --- |
| `/proc/{stat,uptime,loadavg,meminfo,diskstats,net/dev,net/route,net/if_inet6}` | Every Linux kernel |
| `/sys/class/net/<iface>/{mtu,address}` | Every Linux kernel |
| `/etc/{os-release,resolv.conf}` | Every modern Linux distro |
| `statvfs`, `ps`, `systemctl`, `dpkg-query` | Standard userspace |
| Docker container list | Optional, via `/var/run/docker.sock` |
| SMART NVMe data | Optional, via `smartctl -j` |
| UPS battery / runtime | Optional, via `upsc` (NUT) |
| CPU temp, frequency, fan, per-core %, mem%, throttle | Pironman 5 local API (Pi-specific) |
| Tunnel interface (wg0) inside a Docker netns | Optional, via `nsenter` |

If you run it on a generic Linux server without Pironman, CPU temperature and per-core percentages currently come up empty. The rest works as is. Adding a fallback that reads `/proc/stat` plus `/sys/class/thermal/thermal_zone0/temp` is on the roadmap (see below) and is a small change.

## Hacking on it

`worker/src/assets/{html,css,js}.ts` are the entire UI as exported template strings. `wrangler dev` hot-reloads on save.

```sh
cd worker
npx wrangler dev --port 8787 --ip 0.0.0.0
```

Then point your collector at `http://<laptop-lan-ip>:8787/ingest` instead of the deployed URL, restart the systemd unit, and watch the page at `http://localhost:8787/` repaint live as you edit.

## Roadmap

- `--no-pironman` mode for generic Linux servers (reads `/proc/stat` + thermal zones directly)
- Configurable layout (drop/add panels via JSON config, no recompile)
- GPU stats for boxes with discrete GPUs
- Per-process CPU heatmap
- Standalone self-hosted backend (a tiny Go or Node binary that replaces the Cloudflare Worker, for people who don't want the edge dependency)
- Debian + Arch packages so you can `apt install monomi-collector` or `yay -S monomi-collector`
- Container image for the collector

If you want any of these and can write code, please open a PR.

## License

[PolyForm Noncommercial 1.0.0](./LICENSE). You can use, modify, and redistribute monomi for any non-commercial purpose: personal use, home lab, education, research, internal use at non-profits. Commercial use (selling, paid SaaS hosting, paid bundling) is not permitted.

This is _source-available, not OSI open-source_. The distinction matters if you're a lawyer; for everyone else it just means "free for hobbyists, please don't sell my code."

## Credits

This wouldn't exist without [btop](https://github.com/aristocratos/btop) by [@aristocratos](https://github.com/aristocratos). monomi copies no code from btop, but every panel, every gradient, every braille glyph is a tribute to its aesthetic. The colour palette is btop's `Default_theme` transcribed straight from `src/btop_theme.cpp`. If you like monomi, go install btop and stare at it for an hour. The original deserves the love.

Also indebted to:

- [Pironman5](https://github.com/sunfounder/pironman5) for the Pi 5 case telemetry API
- [NUT (Network UPS Tools)](https://networkupstools.org/) for the UPS data plumbing
- [Cloudflare Workers](https://workers.cloudflare.com/) for making the edge free for hobbyists

---

Built for a Raspberry Pi 5, but ready to grow. PRs welcome.
