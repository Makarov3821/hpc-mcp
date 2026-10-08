#!/bin/bash
#BSUB -q Single
#BSUB -n 1
set -eu
mkdir -p results
hostname > results/hostname.txt
date -u > results/time.txt
sleep 60
printf 'Hello from LSF\n'
