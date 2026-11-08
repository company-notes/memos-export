#!/bin/sh
# Stop Memos only; Supervisor keeps this container running.
set -eu

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 --db /var/opt/memos/memos_prod.db --from-user A --to-user B --memo UID --apply --confirm 'A->B'" >&2
  exit 2
fi
if [ "$(id -u)" != "0" ]; then
  echo 'ERROR: run as root (Supervisor control socket is root-only).' >&2
  exit 1
fi
if ! command -v supervisorctl >/dev/null 2>&1; then
  echo 'ERROR: supervisorctl not found; use a Supervisor-managed Memos image first.' >&2
  exit 1
fi
state=$(supervisorctl status memos)
case " $state " in
  *" RUNNING "*) ;;
  *) echo "ERROR: Memos must be RUNNING before this helper is used: $state" >&2; exit 1 ;;
esac

# A manual supervisorctl stop persists until explicitly restarted.
supervisorctl stop memos

restart_memos() {
  code=$?
  trap - 0 2 15
  echo 'Restarting the Memos server via Supervisor...'
  if ! supervisorctl start memos; then
    echo 'ERROR: Memos failed to restart; investigate via supervisorctl status memos and Docker logs.' >&2
    if [ "$code" -eq 0 ]; then code=1; fi
  fi
  exit "$code"
}
trap restart_memos 0
trap 'exit 130' 2
trap 'exit 143' 15

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
python3 "$script_dir/transfer_memo_owner.py" --stopped "$@"
