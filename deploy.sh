#!/bin/bash

USER="pi"
  HOST="${RPI_HOST:-10.70.146.232}"

rsync -a --info=progress2 ./ $USER@$HOST:/home/$USER/toolchain --exclude-from='.gitignore' --delete
# Remove stale user-level install that would shadow the system-wide one
ssh $USER@$HOST 'python3 -m pip uninstall raccoon -y --break-system-packages 2>/dev/null || true'
ssh $USER@$HOST 'cd toolchain && sudo RACCOON_SKIP_WEBIDE=1 python3 -m pip install . --break-system-packages'
# Configure the systemd service
ssh $USER@$HOST 'sudo raccoon-server install'
echo "Deployment to $HOST completed."
ssh $USER@$HOST 'if [ ! -f ~/.raccoon/cli_state.yml ]; then \
  echo "completion_offered: true" > ~/.raccoon/cli_state.yml; \
else \
  grep -q "^completion_offered:" ~/.raccoon/cli_state.yml || echo "completion_offered: true" >> ~/.raccoon/cli_state.yml; \
fi'
ssh $USER@$HOST 'sudo systemctl restart raccoon.service'
