#!/bin/bash
#BSUB -q normal
#BSUB -n 4
#BSUB -R "span[hosts=1]"
#BSUB -W 0:10
set -euo pipefail
export OMP_NUM_THREADS=4
cat < input.txt > results/output.txt 2> results/error.txt
