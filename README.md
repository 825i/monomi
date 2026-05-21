# monomi

物見, _your watchtower, always watching._

A small, fast, browser-accessible status page and resource monitor for Linux. Clearly inspired by [btop](https://github.com/aristocratos/btop), but built to live on a small case-mounted screen or get pulled up in a tab whenever you want to see how your box is doing.

![monomi-dashboard](https://files.catbox.moe/o62kam.png)

The style automatically resizes for slim portrait monitors or PC cases (not pictured here yet).

## What it is

You run one Python process on the machine you want to watch. It does the lot: gathers a /proc + /sys snapshot every couple of seconds, holds a few minutes of rolling history in memory, and serves a self-contained HTML/CSS/JS dashboard on a local port. Your browser polls that port and repaints. To expose it to the outside world you stick a Cloudflare Tunnel (or nginx, or Tailscale, or whatever you already use) in front of it.

That's the whole thing. No database, no Docker required, no Python web framework, no JavaScript build step, no remote backend, no API rate limit. About 2,600 lines of code total. Standard library only on the server side, vanilla JS on the client side.

## Features

- **Live updates every second** (1000 ms cadence default) with sub-100ms repaint, no flicker. Tunable up or down via `INTERVAL`.
- **Braille graphs** in the spirit of btop, with water-reflection mirror mode for CPU and network
- **Per-core sparklines** coloured green to red along btop's CPU gradient
- **Memory + swap as bucket fills** showing each metric as a filling glass
- **Per-pool disk I/O sparkline** (read on top, write on bottom)
- **Multi-interface network**, with LAN IP plus public IP (blurred until hover for privacy)
- **Top 40 processes** by CPU, sorted live
- **System info footer**: OS, kernel, model, arch, GPU, shell, package count, last `apt` run, UPS state
- **Responsive layout** that holds up from 1920px down to mobile
- **Fully self-hosted.** No external service required to run it. Your data never leaves your box unless you put it on the internet yourself.

## Why

I wanted a glanceable status page for my home Pi 5 that I didn't have to SSH in to check. btop is gorgeous but it's a TTY app. Existing web monitors (Glances, Netdata, Cockpit, etc.) felt heavyweight: Python web stacks, gigabytes of historical metrics, paid tiers, big install footprints. I wanted the smallest possible thing that looked good, updated live, ran fully on the host, and could leave open on a small case display without melting the Pi.

## How it works

```
   ┌─────────────────────────────────────────┐
   │             Linux box                   │
   │                                         │
   │   ┌─────────────────────────────────┐   │
   │   │  monomi.py                      │   │
   │   │                                 │   │
   │   │  collector thread ─┐            │   │
   │   │   (every 1 s)       ▼            │   │
   │   │    /proc, /sys ─▶ in-memory     │   │
   │   │    smartctl,      state +       │   │
   │   │    upsc, ps        history      │   │
   │   │                     ▲           │   │
   │   │  HTTP server ───────┘           │   │
   │   │   on 127.0.0.1:8080             │   │
   │   └─────────────────────────────────┘   │
   │                  ▲                      │
   │                  │   /  /api/stats      │
   └──────────────────┼──────────────────────┘
                      │
            ┌─────────┴──────────┐
            │  Any browser       │
            │  polls every 1 s   │
            └────────────────────┘
```

One Python process. The collector thread refreshes shared state; the HTTP server reads it. Browser polls `/api/stats` and repaints. The static HTML/CSS/JS files in `collector/assets/` are served from disk by the same server.

## Tech stack

| Layer | Tech |
| --- | --- |
| Backend | Python 3, standard library only (no `pip install`) |
| Server | `http.server.ThreadingHTTPServer` |
| UI | ~780 lines vanilla JS, ~640 lines CSS, ~160 lines HTML (zero runtime deps) |
| Wire format | JSON over HTTP |
| Cadence | 1 s collector + 1 s browser poll by default (set `INTERVAL` to tune) |
| Service unit | systemd (`Type=simple`, restart on failure) |

## Footprint

| Resource | Cost |
| --- | --- |
| RSS on the host | ~25 MB |
| CPU at 1 s cadence | <1% on a Pi 5 |
| Disk usage | journald log lines only |
| Page weight | one HTML doc, one JS file, one CSS file, served from disk |
| External services | none |

The whole project is built around the constraint that nothing should consume more than a sliver of resources.

## Quick start

Two files go on the host: `monomi.py` and the `assets/` directory next to it. One systemd unit starts it.

From your laptop:

```sh
git clone https://github.com/825i/monomi.git
cd monomi/collector
scp -r monomi.py assets monomi.service monomi.env.example user@host:/tmp/monomi-install/
```

On the host:

```sh
sudo install -d /opt/monomi /etc/monomi
sudo install -m 0755 /tmp/monomi-install/monomi.py /opt/monomi/
sudo cp -r /tmp/monomi-install/assets /opt/monomi/
sudo install -m 0644 /tmp/monomi-install/monomi.service /etc/systemd/system/

sudo cp /tmp/monomi-install/monomi.env.example /etc/monomi/monomi.env
sudo chmod 600 /etc/monomi/monomi.env
# edit /etc/monomi/monomi.env to taste (defaults are sane)

sudo systemctl daemon-reload
sudo systemctl enable --now monomi
sudo systemctl status monomi --no-pager
```

By default the server listens on `127.0.0.1:8080`. Either:

- **LAN access only**: set `BIND_ADDR=0.0.0.0` in `/etc/monomi/monomi.env` and browse to `http://<host-ip>:8080/`.
- **Public access**: put a tunnel or reverse proxy in front of `127.0.0.1:8080`. See the next section.

Total install time: under three minutes.

## Putting it on the public internet

monomi binds to localhost by default, on purpose. To expose it, pick whichever tunnel or reverse proxy you already trust. A few common shapes:

**Cloudflare Tunnel** (great if your IP changes or you don't want to forward ports):

1. Zero Trust → Networks → Tunnels → pick your existing tunnel → Public Hostnames → Add
2. Subdomain: `monomi`, Domain: your zone, Service: `HTTP`, URL: `localhost:8080`
3. Optionally add a Cloudflare Access policy on the same hostname to gate it behind SSO

Because the server serves both the page and the JSON from the same path, you don't need any path-scoped bypass tricks. One Access app, one policy, done.

**Tailscale**: nothing extra needed. `http://<tailscale-name>:8080/` reaches it directly from any device on your tailnet.

**Nginx / Caddy / Traefik**: standard reverse proxy to `127.0.0.1:8080`. monomi sets `Cache-Control: no-store` on the JSON endpoint, so no fiddling required.

## Configuration

`/etc/monomi/monomi.env`:

```
INTERVAL=1.0                                # snapshot cadence (seconds)
BIND_ADDR=127.0.0.1                         # 0.0.0.0 to expose on LAN
PORT=8080
HISTORY_CAP=360                             # 6 minutes at INTERVAL=1.0

# Optional integrations. Comment out if you don't use them
PIRONMAN_URL=http://127.0.0.1:34001/api/v1.0/get-data
NUT_UPS=eaton3s
```

The watched-services list and the pool device list both live at the top of `monomi.py`. Edit for whatever you care about.

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

If you run it on a generic Linux server without Pironman, CPU temperature and per-core percentages currently come up empty. The rest works as is. Adding a fallback that reads `/proc/stat` plus `/sys/class/thermal/thermal_zone0/temp` is on the roadmap and is a small change.

## Hacking on it

`collector/assets/{index.html,style.css,app.js}` are the entire UI. Edit them, restart the service, refresh.

```sh
# locally, with auto-restart on save (Pi5 is fast enough that file-watching is fine)
sudo systemctl restart monomi
```

For ergonomic dev: run `monomi.py` straight from your checkout on the Pi or any Linux box, point your browser at `http://localhost:8080/`, edit the assets, restart. No build step.

## Roadmap

- `--no-pironman` mode for generic Linux servers (reads `/proc/stat` + thermal zones directly)
- Configurable layout (drop/add panels via JSON config, no recompile)
- GPU stats for boxes with discrete GPUs
- Per-process CPU heatmap
- Debian + Arch packages so you can `apt install monomi` or `yay -S monomi`
- Container image
- Optional WebSocket transport instead of polling, for the lowest possible latency

If you want any of these and can write code, please open a PR.

## License

[PolyForm Noncommercial 1.0.0](./LICENSE). You can use, modify, and redistribute monomi for any non-commercial purpose: personal use, home lab, education, research, internal use at non-profits. Commercial use (selling, paid SaaS hosting, paid bundling) is not permitted.

This is _source-available, not OSI open-source_. The distinction matters if you're a lawyer; for everyone else it just means "free for hobbyists, please don't sell my code."

## Credits

This wouldn't exist without [btop](https://github.com/aristocratos/btop) by [@aristocratos](https://github.com/aristocratos). monomi copies no code from btop, but every panel, every gradient, every braille glyph is a tribute to its aesthetic. The colour palette is btop's `Default_theme` transcribed straight from `src/btop_theme.cpp`. If you like monomi, go install btop and stare at it for an hour. The original deserves the love.

Also indebted to:

- [Pironman5](https://github.com/sunfounder/pironman5) for the Pi 5 case telemetry API
- [NUT (Network UPS Tools)](https://networkupstools.org/) for the UPS data plumbing

---

Built for a Raspberry Pi 5, but ready to grow. PRs welcome.
