#!/bin/bash
# Watch, as root, the EC queries the firmware raises, how long the NVIDIA
# driver takes for each NVPCF notification, and the GPU's runtime power
# state. A notification that wakes the GPU takes about 2 seconds; one that
# does not takes microseconds.
#
# usage: trace.sh [seconds]   (default 60)
#
# The function trace runs in a private tracing instance, so the global
# tracer settings stay as they are, and only the EC driver's debug lines that
# are off get turned on. On exit, including on a signal, the instance is
# removed and those lines are turned off again.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
seconds=${1:-60}
gpu=$(nvidia_gpu) || { echo "no NVIDIA GPU found"; exit 1; }
C=/sys/kernel/debug/dynamic_debug/control
I=/sys/kernel/tracing/instances/nvpcf-trace-$$
G=/sys/bus/pci/devices/$gpu/power/runtime_status

# The EC debug lines that are off now; only these are turned on and off.
mapfile -t ec_off < <(awk '$1 ~ /^drivers\/acpi\/ec\.c:/ && $3 !~ /p/ {split($1, a, ":"); print a[2]}' $C)

cleanup() {
    local l
    trap - EXIT INT TERM HUP
    for l in "${ec_off[@]}"; do
        echo "file drivers/acpi/ec.c line $l -p" > $C
    done
    if [ -d $I ]; then
        echo 0 > $I/tracing_on
        echo nop > $I/current_tracer
        rmdir $I
    fi
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

echo "platform profile: $(cat /sys/firmware/acpi/platform_profile)"
mkdir $I || exit 1
echo nv_acpi_nvpcf_event > $I/set_ftrace_filter
echo function_graph > $I/current_tracer
echo 1 > $I/tracing_on
for l in "${ec_off[@]}"; do
    echo "file drivers/acpi/ec.c line $l +p" > $C
done
since=$(date +%Y-%m-%dT%H:%M:%S)
prev=""
end=$(( $(date +%s) + seconds ))
while [ "$(date +%s)" -lt $end ]; do
    s=$(cat $G)
    if [ "$s" != "$prev" ]; then echo "$(date +%T.%N | cut -c1-12) gpu $s"; prev=$s; fi
    sleep 0.1
done
echo "--- EC queries:"
dmesg --time-format iso | grep -E "Query\(0x[0-9a-f]+\) started" | awk -v s="$since" '$1 >= s' \
    | sed -E 's/^[^T]+T([^,]+),([0-9]{3}).*(Query\(0x[0-9a-f]+\)).*/\1.\2 \3/'
echo "--- NVPCF notifications (CPU, duration):"
grep nv_acpi_nvpcf_event $I/trace || echo "none"
