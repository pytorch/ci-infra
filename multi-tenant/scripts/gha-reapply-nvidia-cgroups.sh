#!/usr/bin/env bash
# Waits for the NVIDIA driver to be ready after boot, then restarts each
# per-GPU user's cgroup slice (and rootless docker) so the DeviceAllow
# rules in /etc/systemd/system/user-<uid>.slice re-resolve against the
# real /dev/nvidia* nodes. Mirrors the "restart cgroup slices" step in
# playbooks/restart-services.yml, which is the known-working manual fix
# for pytorch/pytorch#190132 ("No devices were found").
#
# Never fails hard: the goal is to make ghad-manager's startup more
# reliable, not to introduce a new single point of failure that blocks it.
set -u

USERS_FILE=/etc/gha-runner-config/multi-tenant-users
if [ ! -f "$USERS_FILE" ]; then
  echo "gha-reapply-nvidia-cgroups: no $USERS_FILE, nothing to do"
  exit 0
fi

echo "gha-reapply-nvidia-cgroups: waiting for nvidia-smi..."
ready=0
for _ in $(seq 1 60); do
  if nvidia-smi >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" -eq 0 ]; then
  echo "gha-reapply-nvidia-cgroups: nvidia-smi still not responding after 120s, reapplying cgroups anyway" >&2
fi

while IFS= read -r name; do
  [ -n "$name" ] || continue
  uid="$(id -u "$name" 2>/dev/null)" || { echo "gha-reapply-nvidia-cgroups: skip $name, no such user" >&2; continue; }
  echo "gha-reapply-nvidia-cgroups: restarting user-${uid}.slice ($name)"
  systemctl restart "user-${uid}.slice" || echo "gha-reapply-nvidia-cgroups: failed to restart user-${uid}.slice" >&2
  machinectl shell "${name}@" /bin/bash -c 'systemctl --user restart docker' \
    || echo "gha-reapply-nvidia-cgroups: failed to restart docker for $name" >&2
done < "$USERS_FILE"

exit 0
