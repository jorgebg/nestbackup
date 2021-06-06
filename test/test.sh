#!/bin/bash

# Remove previous containers
# -f: Don't ask to confirm removal
# -v: Remove volumes
docker-compose rm -f -v

# -v: Remove volumes
docker-compose down -v

# Run server/app/test.py
# --build: Rebuild server image 
# --abort-on-container-exit: Exit when server finishes the tests
# docker-compose up --build
# docker-compose up --build --abort-on-container-exit
docker-compose up --build --abort-on-container-exit --exit-code-from server
exit $?