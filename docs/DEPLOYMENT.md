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
