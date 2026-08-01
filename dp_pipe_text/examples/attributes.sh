cat > /tmp/dp_status.pipe << 'EOF'
{
  "bg": "#101828",
  "align": "center",
  "valign": "middle",
  "color": "#e0e0e0",
  "size": 14,
  "lines": [
    {"text": "ERROR", "color": "#dc2626", "size": 20, "bold": true},
    "disk 92% full",
    {"text": "check logs", "align": "right", "size": 10}
  ]
}
EOF
