# Sourced by swap.sh and trace.sh.

# Print the PCI address of the NVIDIA GPU: the first display controller with
# NVIDIA's vendor ID. This machine has one.
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
