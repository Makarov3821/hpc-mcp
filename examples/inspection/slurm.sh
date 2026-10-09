#!/bin/bash
#SBATCH --partition=compute
#SBATCH --cpus-per-task=4
#SBATCH --mem=2G
#SBATCH --time=00:10:00
set -euo pipefail
export OMP_NUM_THREADS=4
cat < input.txt > results/output.txt 2> results/error.txt
