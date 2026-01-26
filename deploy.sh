#!/bin/bash

USER="pi"
HOST="${RPI_HOST:-192.168.178.65}"

rsync -a --info=progress2 ./ $USER@$HOST:/home/$USER/toolchain --exclude-from='.gitignore' --delete
ssh $USER@$HOST 'cd toolchain && sudo python3 -m pip install . --break-system-packages'
# Configure the systemd service
ssh $USER@$HOST 'sudo raccoon-server install'
echo "Deployment to $HOST completed."
ssh $USER@$HOST 'sudo systemctl restart raccoon.service'