#!/bin/bash
# Script to reset Machine ID on Linux
# Run with sudo

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root"
   exit 1
fi

echo "Original Machine ID: $(cat /etc/machine-id)"

# 1. Remove the current machine-id files
rm -f /etc/machine-id
rm -f /var/lib/dbus/machine-id

# 2. Generate a new ID
systemd-machine-id-setup

# 3. Link the dbus ID to the new system ID
ln -s /etc/machine-id /var/lib/dbus/machine-id

echo "New Machine ID generated: $(cat /etc/machine-id)"
echo "Please reboot your system to ensure all services pick up the change."

#!/bin/bash
# Script to reset Machine ID on Linux
# Run with sudo

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root" 
   exit 1
fi

echo "Original Machine ID: $(cat /etc/machine-id)"

# 1. Remove the current machine-id files
rm -f /etc/machine-id
rm -f /var/lib/dbus/machine-id

# 2. Generate a new ID
systemd-machine-id-setup

# 3. Link the dbus ID to the new system ID
ln -s /etc/machine-id /var/lib/dbus/machine-id

echo "New Machine ID generated: $(cat /etc/machine-id)"
echo "Please reboot your system to ensure all services pick up the change."
