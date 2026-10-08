#!/bin/bash
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:02:00
set -eu
mkdir -p results
hostname > results/hostname.txt
date -u > results/time.txt
printf 'Hello from Slurm\n'
