# Sourced by swap.sh, trace.sh and install-hook.sh, which set DIR to the
# repository directory.

# install-hook.sh installs these files of the repository in HOOK_LIB, and
# swap.sh refuses to swap while an installed copy differs from the
# repository's. load-hook.sh finds the others next to itself, and
# check_nvidia.py reads the two references next to itself.
HOOK_LIB=/usr/local/lib/nvpcf-hook
HOOK_TOOLS=(load-hook.sh hook/nvpcf-hookctl hook/tests/check_nvidia.py hook/tests/expected_dispatcher.c
            hook/tests/expected_procfs_wrappers.h)

# Print the PCI address of the first display controller with NVIDIA's vendor
# ID.
nvidia_gpu() {
    local d
    for d in /sys/bus/pci/devices/*; do
        [ "$(cat "$d/vendor")" = 0x10de ] || continue
        case "$(cat "$d/class")" in
            0x03*) echo "${d##*/}"; return 0 ;;
        esac
    done
    return 1
}

# nvpcf_hook holds firmware notifications for an NVIDIA driver without patch
# 0001. hook/README.md describes its states and its control client.
hook_loaded() { grep -q '^nvpcf_hook ' /proc/modules; }
hookctl() { "$DIR/hook/nvpcf-hookctl" "$@"; }
# Print one line with nvpcf_hook's state and request counts, or the reason
# they are unknown. Reading the status does not change the hook.
hook_summary() {
    local s
    if ! hook_loaded; then
        echo "nvpcf_hook is not loaded"
    elif ! s=$(hookctl status 2>&1); then
        echo "cannot read the status of nvpcf_hook: $s"
    else
        printf '%s\n' "$s" | awk -F': ' '
            $1 ~ /^(state|pending_requests|received_requests|deferred_requests|dispatch_attempts|requests_dispatched|dispatch_failures|retirement_failed|last_error)$/ {
                line = line (line == "" ? "" : ", ") $1 " " $2
            }
            END { print "nvpcf_hook: " line }'
    fi
}
