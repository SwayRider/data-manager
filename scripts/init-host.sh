#!/bin/bash
#
# init-host.sh - prepare a (new) machine to run data-manager: the shared group, and the package repository with the
# right permissions. Run it once per machine, before the first package or deploy.
#
#   scripts/init-host.sh            report and show the commands that would fix it; changes nothing
#   scripts/init-host.sh --dry-run  same (explicit)
#   scripts/init-host.sh --apply    report, show the commands, then ask:
#                                     A = run them now (root parts go through sudo, which asks for your password)
#                                     M = manual: they are printed again, you run them and press Enter
#                                     Q = quit
#                                   after A or M the checks run again so you see whether it worked
#
# What is shared: the package repository (PACKAGE_ROOT) belongs to root and the group $DATA_GROUP (default swdata),
# setgid, with a default ACL, so every administrator in that group can package, verify and delete whatever another
# one made. The same group is used by the deploy targets: infra/dev-mini/scripts/prepare-host.sh prepares those.
# DATA_ROOT (database, downloads, library, work) stays private to the user that runs data-manager.
#
# Checks: the group (created if missing, you are added, the script continues inside `sg` so it is active), the acl tools,
# docker access (the deploy restarts containers), PACKAGE_ROOT and DATA_ROOT. Paths are read through the application's own
# configuration (environment / .env), nothing else from .env is shown.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GROUP="${DATA_GROUP:-swdata}"
ME="$(id -un)"
APPLY=false
case "${1:-}" in
    "" | --dry-run) ;;
    --apply) APPLY=true ;;
    *) echo "usage: $0 [--dry-run | --apply]" >&2; exit 2 ;;
esac

group_exists()   { getent group "$GROUP" >/dev/null; }
group_has_me()   { getent group "$GROUP" | cut -d: -f4 | tr ',' '\n' | grep -qx "$ME"; }
group_in_shell() { id -nG | tr ' ' '\n' | grep -qx "$GROUP"; }

reexec_in_group() {
    echo "Group $GROUP is not active in this shell; continuing inside it (sg)."
    exec env INIT_HOST_SG=1 sg "$GROUP" -c "$(printf '%q ' "$0" "$@")"
}
if group_exists && group_has_me && ! group_in_shell && [[ -z "${INIT_HOST_SG:-}" ]]; then
    reexec_in_group "$@"
fi

PY="$HERE/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
paths() {  # PACKAGE_ROOT and DATA_ROOT as the application sees them
    (cd "$HERE" && "$PY" -c 'from datamanager.config import config; print(config.package_root); print(config.DATA_ROOT)' 2>/dev/null)
}

RC=0
SYSTEM=() MKDIRS=() TREES=()
PLAN=()
ok()   { echo "  ok    $1"; }
warn() { echo "  WARN  $1"; RC=1; }

nearest_parent() { local p="$1"; while [[ ! -e "$p" && "$p" != "/" ]]; do p="$(dirname "$p")"; done; echo "$p"; }

share() {
    local t="$1"
    TREES+=("sudo chown root:$GROUP '$t'"
            "sudo chmod g+rwx,g+s '$t'"
            "sudo setfacl -m g:$GROUP:rwx -m d:g:$GROUP:rwx '$t'")
}

check_all() {
    RC=0 SYSTEM=() MKDIRS=() TREES=() PLAN=()
    echo "Access"
    if ! group_exists; then warn "the group $GROUP does not exist"; SYSTEM+=("sudo groupadd $GROUP")
    else ok "group $GROUP exists (gid $(getent group "$GROUP" | cut -d: -f3))"; fi
    if ! group_exists || ! group_has_me; then
        warn "$ME is not a member of $GROUP"; SYSTEM+=("sudo usermod -aG $GROUP $ME")
    elif ! group_in_shell; then
        warn "$ME is a member of $GROUP, but this shell does not have the group yet: log in again or run 'newgrp $GROUP'"
    else ok "$ME is a member of $GROUP"; fi
    if command -v setfacl >/dev/null; then ok "setfacl is installed"; else warn "setfacl (package acl) is not installed"; SYSTEM+=("sudo apt-get install -y acl"); fi
    if id -nG | tr ' ' '\n' | grep -qx docker; then ok "$ME can use docker (group docker)"
    else warn "$ME is not in the group docker: the deploy restarts containers"; fi

    local package_root data_root
    { read -r package_root; read -r data_root; } < <(paths)
    echo "Directories"
    if [[ -z "$package_root" ]]; then
        warn "cannot read PACKAGE_ROOT from the application configuration (is the virtualenv set up? see CLAUDE.md)"
    elif [[ "$package_root" =~ [^A-Za-z0-9_./@:+=\ -] ]]; then
        warn "PACKAGE_ROOT contains characters this script will not put in a command: $package_root"
    elif [[ ! -d "$package_root" ]]; then
        warn "PACKAGE_ROOT: $package_root does not exist"
        local parent; parent="$(nearest_parent "$package_root")"
        if [[ -w "$parent" ]]; then MKDIRS+=("mkdir -p '$package_root'"); else MKDIRS+=("sudo mkdir -p '$package_root'"); fi
        share "$package_root"
    elif [[ "$(stat -c %G "$package_root")" != "$GROUP" || ! -g "$package_root" || ! -w "$package_root" ]]; then
        warn "PACKAGE_ROOT: $package_root is not shared through the group $GROUP (group, setgid and write access for you)"
        share "$package_root"
    else
        ok "PACKAGE_ROOT: $package_root"
        local fs; fs="$(df --output=source "$package_root" | tail -1)"
        [[ "$(stat -c %d "$package_root")" == "$(stat -c %d "${data_root:-/}" 2>/dev/null || echo x)" ]] && \
            echo "  note  the repository is on the same filesystem as DATA_ROOT: it saves no SSD space (a separate disk or dataset is advised)"
        printf '  %-18s %s free (%s)\n' PACKAGE_ROOT "$(df -h --output=avail "$package_root" | tail -1 | tr -d ' ')" "$fs"
    fi
    if [[ -n "$data_root" ]]; then
        if [[ -d "$data_root" && -w "$data_root" ]]; then ok "DATA_ROOT: $data_root (private to $ME)"
        elif [[ ! -e "$data_root" ]]; then ok "DATA_ROOT: $data_root does not exist yet (data-manager creates it)"
        else warn "DATA_ROOT: $data_root is not writable for $ME"; fi
    fi

    local seen=$'\n' list cmd
    for list in SYSTEM MKDIRS TREES; do
        declare -n arr="$list"
        for cmd in "${arr[@]}"; do
            if [[ "$seen" != *$'\n'"$cmd"$'\n'* ]]; then PLAN+=("$cmd"); seen+="$cmd"$'\n'; fi
        done
        unset -n arr
    done
}

show_plan() { echo; echo "$1"; printf '  %s\n' "${PLAN[@]}"; }
shell_note() {
    if [[ -n "${INIT_HOST_SG:-}" ]]; then
        echo; echo "Your own shell does not have the group $GROUP yet: log in again, or run 'newgrp $GROUP' before you start"
        echo "./debug.sh or the worker from it (otherwise they cannot write to the shared package repository)."
    fi
}

check_all
if (( ${#PLAN[@]} == 0 )); then
    echo; [[ $RC -eq 0 ]] && echo "Everything is in place." || echo "Nothing this script can fix: see the WARN lines."
    shell_note; exit $RC
fi
if ! $APPLY; then
    show_plan "Commands that would fix this (nothing was changed):"
    echo; echo "Run '$0 --apply' to be asked whether to run them or to do them yourself."
    exit $RC
fi
if [[ ! -t 0 ]]; then
    show_plan "Commands that would fix this:"; echo "Not an interactive terminal: nothing was done." >&2; exit 1
fi
while (( ${#PLAN[@]} )); do
    show_plan "These commands would fix it:"; echo
    read -r -p "[A]pply them now (sudo will ask for your password), do it [M]anually, or [Q]uit? " choice
    case "${choice,,}" in
        a) for cmd in "${PLAN[@]}"; do echo "+ $cmd"; if ! bash -c "$cmd"; then echo "  failed: $cmd" >&2; break; fi; done ;;
        m) show_plan "Run these yourself, in this order:"; echo
           read -r -p "Press Enter when you have run them (q to quit): " done_
           [[ "${done_,,}" == q ]] && exit 1 ;;
        q) exit 1 ;;
        *) echo "Answer A, M or Q."; continue ;;
    esac
    echo
    if group_exists && group_has_me && ! group_in_shell && [[ -z "${INIT_HOST_SG:-}" ]]; then
        echo "The group $GROUP now exists with you in it."
        reexec_in_group
    fi
    echo "Checking again..."
    check_all
    if (( ${#PLAN[@]} == 0 )); then
        echo; [[ $RC -eq 0 ]] && echo "Done: everything is in place." || echo "The commands worked; the remaining WARN lines are not something this script can fix."
        shell_note; exit $RC
    fi
    echo; echo "Something is still missing (see the WARN lines above)."
done
