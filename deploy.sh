#!/bin/bash

USER="pi"
HOST="${RPI_HOST:-192.168.178.65}"

rsync -a --info=progress2 ./ $USER@$HOST:/home/$USER/toolchain --exclude-from='.gitignore' --delete
ssh $USER@$HOST 'cd toolchain && pip install . --break-system-packages'