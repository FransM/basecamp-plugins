#!/bin/bash
PIPE=/tmp/dp_status.pipe
while true; do
  temp=$(sensors | awk '/Package id 0/ {print $4}')
  printf '#!color=#f5c542;size=18;align=center\nCPU\n%s\n' "$temp" > "$PIPE"
  sleep 10
done
