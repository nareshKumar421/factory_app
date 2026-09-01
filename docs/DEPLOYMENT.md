# Deployment

Both apps deploy on a push to `main`, via GitHub Actions over SSH.

| | factory_app | FactoryFlow |
|---|---|---|
| Workflow | `.github/workflows/deploy.yml` | `.github/workflows/deploy.yml` |
| Host secret | `SSH_HOST` | `SERVER_HOST` |
| User secret | `SSH_USERNAME` | `SERVER_USER` |
| Key secret | `SSH_KEY` | `SERVER_SSH_KEY` |
| What runs | `bash factory_deploy.sh` in `/home/superadmin/django_projects` | unpacks the build into `/var/www/react-app`, restarts nginx |

The two repos deploy to the same machine under **different secret names**. Moving
servers means updating all six, or only half the estate follows.

## Moving to a new server

1. Generate a dedicated key with no passphrase — Actions cannot type one:
   `ssh-keygen -t ed25519 -C github-actions-deploy -f ~/.ssh/factory_deploy -N ""`
2. Install the public half in the deploy user's `~/.ssh/authorized_keys`
   (`~/.ssh` 700, `authorized_keys` 600, or sshd ignores it).
3. Prove it before touching GitHub:
   `ssh -i ~/.ssh/factory_deploy -o BatchMode=yes <user>@<host> 'hostname'`.
   `BatchMode=yes` forbids a password fallback, so this passes only if the key works.
4. Open port 22 to the runners. They have dynamic public IPs, so an allow-list of
   the office address blocks them — the failure reads
   `ssh: connect to host ... port 22: Connection timed out`.
5. Set the secrets from the file, never by pasting:
   `gh secret set SSH_KEY -R <owner>/<repo> < ~/.ssh/factory_deploy`.
   A key mangled in a web form fails later as `error in libcrypto`.
6. On the server: `/home/superadmin/django_projects/factory_deploy.sh` must exist
   for the Django side, and the deploy user needs passwordless sudo for `rm`,
   `mkdir`, `tar`, `test` and `systemctl restart nginx` for the React side —
   without it the SSH step waits on a password prompt nobody can answer.

Host keys need no preparation: both workflows run `ssh-keyscan` and connect with
`StrictHostKeyChecking=accept-new`.

## Server-side configuration that does not travel

`.env` lives on the server and is not in this repository, so a new machine starts
without it. At minimum check `HANA_HOST`/`HANA_USER`/`HANA_PASSWORD`, `SL_URL` and
the database settings — a stale one of these fails quietly rather than loudly: the
app runs, and item names simply stop appearing.

## Verified

The backend pipeline was confirmed end to end on 21 Aug 2026, after the server
moved: run 32449284299 deployed in 55s once port 22 accepted the runner. The two
failures before it were the firewall, not the key — a rejected key fails in under
a second, a blocked port takes the full 60s timeout.

## When a run fails with `Connection timed out`

**Since 28 Aug 2026 this is not intermittent and re-running will not help.** The
edge drops inbound traffic to `138.252.101.117` from GitHub's runners, on *every*
port, so the server never learns the connection was attempted. Confirmed 31 Aug
2026 by probing the same host from two places at once:

| Port | From the office (`223.178.211.52`) | From a runner (`172.208.153.209`, Azure) |
|---|---|---|
| 22 | connected, 0s | timed out, 25s |
| 80 | connected, 1s | timed out, 25s |
| 443 | connected, 0s | timed out, 25s |

Run that probe yourself with the **Connectivity check (deploy server)** workflow
(Actions → Run workflow); it prints this table and the runner's public IP.

**Confirmed cause: a GeoIP block. Every non-Indian address is refused; Indian
addresses are allowed.** GitHub-hosted runners are Azure VMs in the United States
(`Iowa` and `Wyoming` on the runs so far), so they are refused by design, and no
change to the workflow, the key or the secrets can alter that.

Ordinary users are **not** affected — the app works on office and mobile networks
alike.

### It is not about SSH keys versus passwords

A tempting theory is that only password logins were restricted and a key would
still get through. It cannot be: the same probe timed out on **TCP 80 and 443**,
ports with no SSH, no usernames and no authentication of any kind. A rule about
sshd auth methods cannot drop HTTPS packets. The connection fails at TCP connect,
before any auth method is negotiated, so sshd never sees it and cannot tell a key
from a password. A key-based deploy from GitHub is blocked exactly as hard.

### Why the error is a timeout, and why that rules the key out

A firewall can refuse traffic two ways and they look nothing alike to the client:

| Where it fails | What happens | What the runner sees | How long |
|---|---|---|---|
| Gateway, DROP | packet silently discarded | `Connection timed out` | the full `ConnectTimeout` |
| Gateway, REJECT | RST / ICMP unreachable sent back | `Connection refused` | instant |
| sshd reached | handshake completes, auth offered | `Permission denied (publickey)` | <1s |
| Key expired/removed | same as above | `Permission denied (publickey)` | <1s |

`Prepare SSH` burning a full 60s is `ssh-keyscan -T 60` timing out while merely
asking for the host key — *before any key is offered*. So a key or secret problem
cannot produce this error, and it would appear in `auth.log`, which it does not.

Allow-listing GitHub is not a fix: runners are ephemeral Azure VMs with a
different public IP every job (~4,000 CIDR blocks that change weekly, in shared
Azure space). Two runs on 31 Aug came from `20.118.221.165` and
`172.208.153.209`.

### Ruling out the box (all checked, all clean)

| Suspect | How to rule it out |
|---|---|
| Host firewall | `sudo ufw status` (inactive), `sudo iptables -S INPUT` (policy ACCEPT; only a Zabbix rule for 10050), `sudo nft list ruleset` (the rest is Docker's) |
| Ban daemon | `systemctl is-active fail2ban crowdsec sshguard` — all absent |
| sshd | `sudo sshd -T` — listening on `0.0.0.0:22`, `pubkeyauthentication yes`, `maxstartups 100:30:300` (too generous to be dropping anything) |
| Routing | `ip route` — one default via `10.10.101.225` on `ens18`, no policy routing, no second live interface |
| The key | `sudo grep -a 'Accepted publickey' /var/log/auth.log*` — succeeds daily from allowed sources |

Use `grep -a`. Mixing `zcat` output with plain logs makes grep call the stream
binary and silently stop printing matches, which reads as "logins stopped" when
they did not — that mistake was made once already while diagnosing this.

### Fixing it properly

The block is doing its job — it was aimed at the brute-force flood and cut it
roughly sixfold — so the goal is to get deploys through *without* asking for it to
be loosened. Ranked by how little they give up:

1. **A tunnel out of the box.** Cloudflare Tunnel (or similar) dials *outward*, so
   nothing inbound is opened and the GeoIP rule is untouched. Someone already
   started this: `/tmp/setup-cf-tunnel.sh`, 22 Aug. Almost certainly the intended
   answer, and worth finishing before building anything new.
2. **Pull-based delivery.** A timer on the box polls `origin/main` and runs the
   same two scripts below. Outbound to GitHub works (`github.com` 200 in 0.28s).
   No inbound, no exception, no tunnel — but no deploy status in GitHub either.
3. **A jump host on an Indian address.** Actions connects to it, it connects here.
   Keeps the current workflows nearly as-is, at the cost of another machine to own.

Not viable: allow-listing GitHub (thousands of Azure ranges, different every run)
and a self-hosted runner (both repositories are public, so anyone's pull request
would run code on this box).

Whatever route is chosen, turn off `PasswordAuthentication` first. It is why the
flood was worth an attacker's time, and with it off the box is safer than it was
before 28 Aug regardless of what the gateway does.

## Deploying by hand when Actions cannot

Two scripts on the server, one per app. Both are release-based: they build into
a fresh `releases/<ts>-<sha>/`, validate before touching what is live, and keep
the previous release for an instant rollback.

```bash
ssh superadmin@138.252.101.117

# backend  — fetch origin/main, per-release venv, check, migrate, collectstatic,
#            atomic symlink swap, health check, auto-rollback on failure
cd /home/superadmin/django_projects && bash factory_deploy.sh deploy
bash factory_deploy.sh status      # release, health, commit vs origin, pending migrations
bash factory_deploy.sh rollback

# frontend — fetch origin/main, npm ci, vite build, validate the bundle,
#            publish to /var/www/react-app, reload nginx
bash /home/superadmin/django_projects/factoryflow_deploy.sh deploy
bash /home/superadmin/django_projects/factoryflow_deploy.sh status
bash /home/superadmin/django_projects/factoryflow_deploy.sh rollback
```

Both deploy whatever is on `origin/main`, so push first.

`status` is read-only, takes a couple of seconds, and reads the same on both
scripts. It answers the questions worth asking before and after a deploy:

* **is the service up** — process state and a real HTTP check (the frontend's goes
  through `https://ji.jivo.in/`, not `127.0.0.1`, which would hit a different site).
* **is the code current** — the deployed commit next to `origin/main`'s, with that
  commit's subject line, then `up to date` or `BEHIND by N`. Short SHAs are
  sometimes all digits (`1641922` is a commit, not a count), which is why the
  subject is printed beside it. Both scripts **fetch first**: comparing against a
  stale ref would let `status` report "up to date" when it is not.
* **does the schema match the code** (backend) — `migrations: all applied`, or
  `N UNAPPLIED`. Needs a database round trip, so it is capped at 45s and degrades
  to `could not check` rather than hanging or failing.

The frontend has no `current` symlink to read, because publishing copies `dist`
into the webroot rather than repointing a link. It identifies the live release by
matching the **served** bundle filename against each release's own build — which
also means it will say so if the webroot matches no build on the box, e.g. after
someone copied files in by hand.

### What the frontend script guards that the workflow does not

* **It validates the built bundle before publishing.** Two build-time values are
  unsafe to default: `VITE_API_BASE_URL` falls back to `http://localhost:8000`
  (an app that cannot reach the API) and `VITE_FIREBASE_VAPID_KEY` has *no*
  fallback, so a build without it silently disables push notifications for
  everyone. The script greps the compiled bundle for both and refuses to publish
  if either is missing. They live in
  `/home/superadmin/react_projects/factoryflow/shared/frontend.env`, and must be
  kept in step with the GitHub repo secrets or the next CI deploy will undo a
  change made here.
* **It keeps the live site until the new one is proven.** The workflow
  `rm -rf`s the served directory *before* it knows the new bundle unpacks; this
  copies the tree to `/tmp/react-app-backup-<ts>` first and restores it if
  `index.html` does not appear.
* **It reloads nginx rather than restarting it.** nginx also fronts the API and
  other sites on this box.
* **It verifies through the real hostname.** A request to `127.0.0.1` hits the
  default server, which is a different site; the check uses
  `curl --resolve ji.jivo.in:443:127.0.0.1`.

Node 22 and npm are already installed on the server, and both repositories are
publicly readable, so neither script needs credentials.

Verified end to end on 31 Aug 2026: `a1fa01eb` built on the box and published as
`assets/index-s1HjBRek.js`, with `ji.jivo.in` returning 200.

### If you build on a laptop instead

The workflow's `VITE_*` values live only in GitHub secrets. Recover the two that
matter from the bundle already being served rather than guessing:

```bash
ssh <user>@<host> "grep -rhoE 'https?://[a-zA-Z0-9._:-]+/api/v1' /var/www/react-app/assets/*.js | sort -u"
ssh <user>@<host> "grep -rhoE '\"B[A-Za-z0-9_-]{80,95}\"' /var/www/react-app/assets/*.js | sort -u"
```

The VAPID grep returns two keys. The one to use is **not** the one beginning
`BDOU99-` — that is a constant shipped inside firebase-js-sdk itself.
