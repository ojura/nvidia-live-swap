#!/bin/bash
# Watch for 60 seconds, as root: the EC queries the firmware raises, how long
# the NVIDIA driver takes for each NVPCF notification, and the GPU's runtime
# power state. A notification that wakes the GPU takes about 2 seconds; one
# that does not takes microseconds.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/lib.sh"
gpu=$(nvidia_gpu) || { echo "no NVIDIA GPU found"; exit 1; }
C=/sys/kernel/debug/dynamic_debug/control
T=/sys/kernel/tracing
G=/sys/bus/pci/devices/$gpu/power/runtime_status
echo "platform profile: $(cat /sys/firmware/acpi/platform_profile)"
echo 0 > $T/tracing_on; echo > $T/trace
echo function_graph > $T/current_tracer
echo nv_acpi_nvpcf_event > $T/set_graph_function
echo 1 > $T/max_graph_depth
echo 1 > $T/tracing_on
echo "file drivers/acpi/ec.c +p" > $C
since=$(date +%Y-%m-%dT%H:%M:%S)
prev=""; end=$(( $(date +%s) + 60 ))
while [ "$(date +%s)" -lt $end ]; do
    s=$(cat $G)
    if [ "$s" != "$prev" ]; then echo "$(date +%T.%N | cut -c1-12) gpu $s"; prev=$s; fi
    sleep 0.1
done
echo "file drivers/acpi/ec.c -p" > $C
echo 0 > $T/tracing_on
echo "--- EC queries:"
dmesg --time-format iso | grep -E "Query\(0x[0-9a-f]+\) started" | awk -v s="$since" '$1 >= s' \
    | sed -E 's/^[^T]+T([^,]+),([0-9]{3}).*(Query\(0x[0-9a-f]+\)).*/\1.\2 \3/'
echo "--- NVPCF notifications (CPU, duration):"
grep nv_acpi_nvpcf_event $T/trace
echo nop > $T/current_tracer; echo > $T/set_graph_function; echo > $T/trace
