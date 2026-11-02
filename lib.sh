# Sourced by swap.sh and trace.sh, which set DIR to the repository directory.

# Print the PCI address of the NVIDIA GPU, the first display controller with
# NVIDIA's vendor ID. This machine has only one.
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
