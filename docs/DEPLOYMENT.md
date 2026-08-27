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

This is **intermittent, not a broken pipeline**, and the instinct to go looking
for a bad key wastes the most time. On 27 Aug 2026 a run failed this way at
~18:30 while deploys landed successfully at 16:00, 18:23 and 18:55 the same day.
Key logins run at 40–75 a day on this box.

Read the timings before anything else:

* **`Prepare SSH` took a full 1m 0s.** That is `ssh-keyscan -T 60` timing out
  before a key is ever offered. Authentication is not reached, so the key, the
  secret and `authorized_keys` are all irrelevant to this failure.
* `Upload Build` then burns ~6m on five 60s connects. The backend workflow has
  **no retry at all** — one timeout kills it.

What it is *not*, all checked on the box:

| Suspect | How to rule it out |
|---|---|
| Host firewall | `sudo ufw status` (inactive), `sudo iptables -S INPUT` (policy ACCEPT; only a Zabbix rule for 10050), `sudo nft list ruleset` (the rest is Docker's) |
| Ban daemon | `systemctl is-active fail2ban crowdsec sshguard` — all absent |
| sshd | `sudo sshd -T \| grep -E 'listenaddress\|pubkey'` — listening on `0.0.0.0:22`, keys enabled |
| The key | `sudo grep -a 'Accepted publickey' /var/log/auth.log* \| cut -dT -f1 \| sort \| uniq -c` — successes every day, including the day of the failure |

Use `grep -a`. Mixing `zcat` output with plain logs makes grep call the stream
binary and silently stop printing matches, which reads as "logins stopped on the
17th" when they did not.

**The likely cause is upstream pressure, not policy.** The box is NAT'd — private
`10.10.101.117` behind public `138.252.101.117` — so an edge device owns port 22,
and that device is under constant load: 16,450 failed password attempts from 234
distinct source IPs in a single day, because `PasswordAuthentication` is still
`yes`. Timeouts that come and go on a flooded NAT point at connection-tracking
pressure or edge rate-limiting. Turning off password auth and putting fail2ban in
front of sshd removes the flood; a self-hosted runner or a pull-based deploy
(a timer on the box running `factory_deploy.sh`) removes the inbound dependency
altogether.

First move on a red run is simply to **re-run the job**.

## Deploying by hand when Actions cannot

Both halves can be driven from a laptop with SSH access. This is the exact path
used on 27 Aug 2026 to ship `c1bc87f` / `b13b66f6`.

**Backend** — the server script does everything (fresh release dir, per-release
venv, `check` before the live service is touched, migrate, collectstatic, atomic
symlink swap, health check, auto-rollback):

```bash
ssh <user>@<host> 'cd /home/superadmin/django_projects && bash factory_deploy.sh deploy'
```

It deploys whatever is on `origin/main`, so push first. `bash factory_deploy.sh
rollback` reverts to the previous release.

**Frontend** — there is no Node on the server, so the bundle is built locally.
The build-time `VITE_*` values live only in GitHub secrets, and two of them
matter:

* `VITE_API_BASE_URL` — falls back to `http://localhost:8000/api/v1`, which
  produces a bundle that cannot talk to anything.
* `VITE_FIREBASE_VAPID_KEY` — has **no** fallback, so a build without it
  silently disables push notifications for every user.

The Firebase app config does have fallbacks in `firebase.config.ts` and they
match production, so nothing else is needed. Recover the two live values from the
bundle currently being served rather than guessing — both are values the app
already ships to every browser:

```bash
ssh <user>@<host> "grep -rhoE 'https?://[a-zA-Z0-9._:-]+/api/v1' /var/www/react-app/assets/*.js | sort -u"
ssh <user>@<host> "grep -rhoE '\"B[A-Za-z0-9_-]{80,95}\"' /var/www/react-app/assets/*.js | sort -u"
```

The VAPID grep returns two keys. The one to use is **not** the one beginning
`BDOU99-` — that is a constant shipped inside firebase-js-sdk itself.

Then build, package and install:

```bash
VITE_API_BASE_URL=<recovered> VITE_FIREBASE_VAPID_KEY=<recovered> npm run build
tar -czf build.tar.gz dist
scp build.tar.gz <user>@<host>:/tmp/build.tar.gz
```

On the server, copy the live tree aside **before** unpacking. The workflow does
not: it `rm -rf`s the served directory before it knows the new bundle is good, so
a bad unpack leaves the site with nothing to serve and no way back.

```bash
sudo cp -a /var/www/react-app /tmp/react-app-backup-$(date +%Y%m%d%H%M%S)
sudo rm -rf /var/www/react-app/*
sudo tar -xzf /tmp/build.tar.gz -C /var/www/react-app --strip-components=1
sudo test -f /var/www/react-app/index.html   # restore the backup if this fails
sudo systemctl restart nginx
```

Verify by the name the browser uses, not by `127.0.0.1` — bare HTTP redirects
(301) and the default server is not this site:

```bash
curl -sk --resolve ji.jivo.in:443:127.0.0.1 https://ji.jivo.in/ | grep -o 'assets/index-[A-Za-z0-9_-]*\.js'
```

That hash must match the one in your local `dist/`. To confirm the backend
release rather than the bundle, call an endpoint you just shipped: `401` means
the route exists and wants auth, `404` means the release did not land.
