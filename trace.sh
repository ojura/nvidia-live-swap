#!/bin/bash
# Watch, as root, the EC queries the firmware raises, how long the NVIDIA
# driver takes for each NVPCF notification, and the GPU's runtime power
# state. A notification that wakes the GPU takes about 2 seconds; one that
# leaves it asleep takes microseconds.
#
# usage: trace.sh [seconds]   (default 60)
#
# The function trace runs in a private tracing instance, so the global
# tracer settings stay as they are, and only the EC driver's debug lines that
# are off get turned on. Every write to a control file is checked, and the
# trace starts only after the instance's function filter reads back as
# nv_acpi_nvpcf_event alone; with an empty filter, function_graph would trace
# every kernel function. On exit, including on a signal, the instance is
# removed and the debug lines it turned on are turned off again.
set -u
usage() { echo "usage: trace.sh [seconds]" >&2; exit 2; }
[ $# -le 1 ] || usage
seconds=${1:-60}
case "$seconds" in
    '' | *[!0-9]*) usage ;;
esac
seconds=$((10#$seconds))  # a leading zero would otherwise mean octal
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
gpu=$(nvidia_gpu) || { echo "cannot find the NVIDIA GPU"; exit 1; }
C=/sys/kernel/debug/dynamic_debug/control
I=/sys/kernel/tracing/instances/nvpcf-trace-$$
G=/sys/bus/pci/devices/$gpu/power/runtime_status

[ -r "$C" ] && [ -w "$C" ] || { echo "cannot read and write $C"; exit 1; }
grep -q '^drivers/acpi/ec\.c:' "$C" || { echo "cannot find the EC driver's debug lines in $C"; exit 1; }
# The EC debug lines that are off now; only these are turned on and off.
mapfile -t ec_off < <(awk '$1 ~ /^drivers\/acpi\/ec\.c:/ && $3 !~ /p/ {split($1, a, ":"); print a[2]}' "$C")

cleanup() {
    local l
    trap - EXIT INT TERM HUP
    for l in "${ec_off[@]}"; do
        echo "file drivers/acpi/ec.c line $l -p" > "$C" || echo "could not turn EC debug line $l off again"
    done
    if [ -d "$I" ]; then
        echo 0 > "$I/tracing_on"
        echo nop > "$I/current_tracer"
        rmdir "$I" || echo "could not remove $I"
    fi
}
fail() { echo "$*"; exit 1; }
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

echo "platform profile: $(cat /sys/firmware/acpi/platform_profile)"
mkdir "$I" || fail "could not create the tracing instance $I"
echo nv_acpi_nvpcf_event > "$I/set_ftrace_filter" || fail "could not set the function filter"
filter=$(cat "$I/set_ftrace_filter") || fail "could not read the function filter back"
if [ "${filter%% *}" != nv_acpi_nvpcf_event ] || [ "$(printf '%s\n' "$filter" | wc -l)" != 1 ]; then
    fail "the function filter reads '$filter' instead of nv_acpi_nvpcf_event"
fi
echo function_graph > "$I/current_tracer" || fail "could not select the function_graph tracer"
echo 1 > "$I/tracing_on" || fail "could not start the trace"
for l in "${ec_off[@]}"; do
    echo "file drivers/acpi/ec.c line $l +p" > "$C" || fail "could not turn EC debug line $l on"
done

since=$(date +%Y-%m-%dT%H:%M:%S)
prev=""
end=$(( $(date +%s) + seconds ))
while [ "$(date +%s)" -lt $end ]; do
    s=$(cat "$G")
    if [ "$s" != "$prev" ]; then echo "$(date +%T.%N | cut -c1-12) gpu $s"; prev=$s; fi
    sleep 0.1
done
# Stop both sources before printing, so the output covers exactly the sample.
echo 0 > "$I/tracing_on" || fail "could not stop the trace"
for l in "${ec_off[@]}"; do
    echo "file drivers/acpi/ec.c line $l -p" > "$C" || fail "could not turn EC debug line $l off again"
done
ec_off=()  # turned off already; the EXIT trap leaves them as they are
records=$(grep nv_acpi_nvpcf_event "$I/trace")
echo "--- EC queries:"
dmesg --time-format iso | grep -E "Query\(0x[0-9a-f]+\) started" | awk -v s="$since" '$1 >= s' \
    | sed -E 's/^[^T]+T([^,]+),([0-9]{3}).*(Query\(0x[0-9a-f]+\)).*/\1.\2 \3/'
echo "--- NVPCF notifications (CPU, duration):"
[ -z "$records" ] || printf '%s\n' "$records"
echo "$(printf '%s' "$records" | grep -c nv_acpi_nvpcf_event) notifications"
